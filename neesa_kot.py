"""
NeeSa KoT (King of Time) クライアント - 勤怠ボード色付け用。
既存 kot_api.py（トレコレKoT）とは別アカウント。NEESA_KOT_TOKEN を使用。
"""
import os
import logging
import requests
from datetime import timezone, timedelta
from dotenv import load_dotenv
import kot_api  # parse_timerecords_for_employee を流用
import neesa_lw  # DEPT_MAP / DEFAULT_GROUP / 並び順

logger = logging.getLogger(__name__)
load_dotenv()

TOKEN = os.getenv("NEESA_KOT_TOKEN")
BASE = "https://api.kingtime.jp/v1.0"
JST = timezone(timedelta(hours=9))


def _get(path):
    try:
        r = requests.get(
            BASE + path,
            headers={"Authorization": "Bearer " + (TOKEN or ""),
                     "Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error("NeeSa KoT取得失敗 %s: %s", path, e)
        return None


def get_employees():
    d = _get("/employees")
    if not d:
        return []
    return d if isinstance(d, list) else d.get("employees", [])


def _start_minutes(start_str):
    """'9'->540, '930'->570, '9:30'->570, '1015'->615 / 解析不能はNone"""
    if not start_str:
        return None
    s = start_str.replace(":", "")
    if not s.isdigit():
        return None
    if len(s) <= 2:
        h, m = int(s), 0
    elif len(s) == 3:
        h, m = int(s[0]), int(s[1:])
    else:
        h, m = int(s[:2]), int(s[2:])
    return h * 60 + m


def _status_for(clock, start_str, now):
    """打刻と予定開始から状態を判定。
    退勤済=done / 出勤中=working / 開始15分超過で未打刻=error / それ以外=scheduled"""
    co = clock.get("clock_out")
    ci = clock.get("clock_in")
    if co:
        return "done"
    if ci:
        return "working"
    sm = _start_minutes(start_str)
    if sm is not None:
        nowm = now.hour * 60 + now.minute
        if nowm > sm + 15:
            return "error"  # 未打刻超過＝要確認
    return "scheduled"


def _clock_by_lastname(emps, clock):
    """lastName -> {clock_in, clock_out}。同姓は打刻ありを優先。"""
    byln = {}
    for e in emps:
        ln = e.get("lastName", "")
        c = clock.get(e.get("key", ""), {})
        info = {"clock_in": c.get("clock_in"), "clock_out": c.get("clock_out")}
        if ln not in byln or (info["clock_in"] and not byln[ln]["clock_in"]):
            byln[ln] = info
    return byln


def apply_today(groups, now):
    """当日のみ: KoT打刻で各シフトのstatusを設定し、
    『打刻のみ（シフト未登録で出勤打刻あり）』の人をグループに追加する。"""
    emps = get_employees()
    tr = _get("/daily-workings/timerecord/" + now.strftime("%Y-%m-%d"))
    # KoT取得失敗（禁止時間帯403・障害等）時は色付けせず予定表示のまま返す
    # （ここで赤=未打刻超過を付けると禁止帯に全員赤になる誤検知を防ぐ）
    if not emps or tr is None:
        logger.warning("NeeSa KoT未取得のため色付けスキップ（予定表示）")
        return groups
    clock = kot_api.parse_timerecords_for_employee(tr)
    byln = _clock_by_lastname(emps, clock)

    # 1) スケジュール者に status 付与
    scheduled = set()
    for g in groups:
        for s in g["shifts"]:
            scheduled.add(s["name"])
            s["status"] = _status_for(byln.get(s["name"], {}), s.get("start"), now)

    # 2) 打刻のみ者（出勤打刻あり＆本日シフト無し）を追加
    gmap = {(g["company"], g["dept"]): g for g in groups}
    added = set()
    for e in emps:
        ln = e.get("lastName", "")
        c = clock.get(e.get("key", ""), {})
        if not c.get("clock_in") or ln in scheduled or ln in added:
            continue
        added.add(ln)
        s = {"name": ln, "start": None, "end": None, "summary": "打刻のみ",
             "remote": ln in neesa_lw.REMOTE_NAMES, "punch_only": True,
             "status": "done" if c.get("clock_out") else "working"}
        key = neesa_lw.DEPT_MAP.get(ln, neesa_lw.DEFAULT_GROUP)
        if key not in gmap:
            ng = {"company": key[0], "dept": key[1], "shifts": []}
            groups.append(ng)
            gmap[key] = ng
        gmap[key]["shifts"].append(s)

    # 3) 並び替え（会社→部署→打刻のみは末尾、時刻順）
    def _co(c):
        return neesa_lw.COMPANY_ORDER.index(c) if c in neesa_lw.COMPANY_ORDER else 99

    def _de(d):
        return neesa_lw.DEPT_ORDER.index(d) if d in neesa_lw.DEPT_ORDER else 99

    groups.sort(key=lambda g: (_co(g["company"]), _de(g["dept"]), g["dept"]))
    for g in groups:
        g["shifts"].sort(key=lambda s: (s.get("start") is None, s.get("start") or ""))
    return groups
