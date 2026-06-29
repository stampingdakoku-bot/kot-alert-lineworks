"""
NeeSa KoT (King of Time) クライアント - 勤怠ボード色付け用。
既存 kot_api.py（トレコレKoT）とは別アカウント。NEESA_KOT_TOKEN を使用。
状態は done(退勤済) / working(出勤中) / scheduled(予定) の3種（要確認=赤は廃止）。
"""
import os
import logging
import requests
from datetime import timezone, timedelta
from dotenv import load_dotenv
import kot_api      # parse_timerecords_for_employee 流用 + トレコレKoT(宮崎)取得
import neesa_lw     # マッピング/設定

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


def _status(info):
    """打刻情報から状態を判定。
    退勤打刻あり or totalWork>0(修正申告等で確定) → done(退勤済)
    出勤打刻あり(未確定) → working(出勤中) / それ以外 → scheduled(予定)"""
    if info.get("clock_out") or (info.get("total_work") or 0) > 0:
        return "done"
    if info.get("clock_in"):
        return "working"
    return "scheduled"


def _build_lastname_map(emps, clock, twmap):
    """lastName -> {clock_in, clock_out, total_work}。
    KOT_FULLNAME指定の同姓は該当フルネームのみ採用。それ以外の同姓は打刻ありを優先。"""
    byln = {}
    for e in emps:
        ln = e.get("lastName", "")
        full = ln + e.get("firstName", "")
        pin = neesa_lw.KOT_FULLNAME.get(ln)
        if pin and full != pin:
            continue  # 同姓の別人は除外（指定フルネームのみ）
        key = e.get("key", "")
        c = clock.get(key, {})
        info = {"clock_in": c.get("clock_in"), "clock_out": c.get("clock_out"),
                "total_work": twmap.get(key)}
        if ln not in byln or (info["clock_in"] and not byln[ln]["clock_in"]):
            byln[ln] = info
    return byln


def _trecole_status(names, date_str):
    """トレコレKoT(既存KOT_TOKEN)から指定名字の当日打刻を取得 {lastName: info}"""
    try:
        emps = kot_api.get_employees() or []
        emps = emps if isinstance(emps, list) else emps.get("employees", [])
        tr = kot_api.get_timerecords(date_str)
        clock = kot_api.parse_timerecords_for_employee(tr) if tr else {}
        dwd = kot_api.get_daily_workings(date_str) or {}
        twmap = {w["employeeKey"]: w.get("totalWork") for w in dwd.get("dailyWorkings", [])}
    except Exception as e:
        logger.error("トレコレKoT取得失敗: %s", e)
        return {}
    out = {}
    for e in emps:
        ln = e.get("lastName", "")
        if ln not in names:
            continue
        key = e.get("key", "")
        c = clock.get(key, {})
        info = {"clock_in": c.get("clock_in"), "clock_out": c.get("clock_out"),
                "total_work": twmap.get(key)}
        if ln not in out or (info["clock_in"] and not out[ln]["clock_in"]):
            out[ln] = info
    return out


def apply_today(groups, now):
    """当日のみ: KoT打刻で各シフトのstatusを設定し、打刻のみ者・宮崎(トレコレ)を追加。"""
    date_str = now.strftime("%Y-%m-%d")
    emps = get_employees()
    tr = _get("/daily-workings/timerecord/" + date_str)
    dwd = _get("/daily-workings/" + date_str)
    # KoT取得失敗（禁止時間帯403等）時は色付けせず予定表示のまま返す
    if not emps or tr is None:
        logger.warning("NeeSa KoT未取得のため色付けスキップ（予定表示）")
        return groups
    clock = kot_api.parse_timerecords_for_employee(tr)
    twmap = {w["employeeKey"]: w.get("totalWork")
             for w in (dwd or {}).get("dailyWorkings", [])}
    byln = _build_lastname_map(emps, clock, twmap)

    # 1) スケジュール者に status 付与（山藤等は打刻無視でスケジュール緑）
    scheduled = set()
    for g in groups:
        for s in g["shifts"]:
            scheduled.add(s["name"])
            if s["name"] in neesa_lw.SCHEDULE_GREEN_NAMES:
                s["status"] = "working"
            else:
                s["status"] = _status(byln.get(s["name"], {}))

    gmap = {(g["company"], g["dept"]): g for g in groups}

    def _add_punch_only(name, info, remote=False):
        if name in scheduled:
            return
        st = _status(info)
        if st == "scheduled":  # 打刻も確定も無いなら出さない
            return
        s = {"name": name, "start": None, "end": None, "summary": "打刻のみ",
             "remote": remote, "punch_only": True, "status": st}
        key = neesa_lw.DEPT_MAP.get(name, neesa_lw.DEFAULT_GROUP)
        if key not in gmap:
            ng = {"company": key[0], "dept": key[1], "shifts": []}
            groups.append(ng)
            gmap[key] = ng
        gmap[key]["shifts"].append(s)
        scheduled.add(name)

    # 2) NeeSa KoT 打刻のみ者（出勤打刻あり＆本日シフト無し）を追加
    for ln, info in byln.items():
        if info.get("clock_in") or (info.get("total_work") or 0) > 0:
            _add_punch_only(ln, info, remote=(ln in neesa_lw.REMOTE_NAMES))

    # 3) 宮崎など トレコレKoT で打刻する人を取得して追加
    if neesa_lw.CROSS_KOT_NAMES:
        for ln, info in _trecole_status(neesa_lw.CROSS_KOT_NAMES, date_str).items():
            _add_punch_only(ln, info)

    # 4) 並び替え（会社→部署→時刻順／打刻のみは末尾）
    def _co(c):
        return neesa_lw.COMPANY_ORDER.index(c) if c in neesa_lw.COMPANY_ORDER else 99

    def _de(d):
        return neesa_lw.DEPT_ORDER.index(d) if d in neesa_lw.DEPT_ORDER else 99

    groups.sort(key=lambda g: (_co(g["company"]), _de(g["dept"]), g["dept"]))
    for g in groups:
        g["shifts"].sort(key=lambda s: (s.get("start") is None, s.get("start") or ""))
    return groups
