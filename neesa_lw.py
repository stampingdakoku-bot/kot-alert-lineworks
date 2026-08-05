"""
NeeSa LINE WORKS カレンダー読み取りクライアント（勤怠ボード用）
既存 lw_api.py とは別テナント（合同会社NeeSa / works-42585）の認証情報を使う。
"""
import os
import re
import time
import logging
import requests
import jwt
from datetime import datetime, timedelta, timezone
from dateutil import rrule as _rrule
from dateutil.parser import isoparse
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

CLIENT_ID = os.getenv("NEESA_LW_CLIENT_ID")
CLIENT_SECRET = os.getenv("NEESA_LW_CLIENT_SECRET")
SERVICE_ACCOUNT = os.getenv("NEESA_LW_SERVICE_ACCOUNT")
PRIVATE_KEY_PATH = os.getenv("NEESA_LW_PRIVATE_KEY_PATH")

AUTH_URL = "https://auth.worksmobile.com/oauth2/v2.0/token"
API_BASE = "https://www.worksapis.com/v1.0"
JST = timezone(timedelta(hours=9))

# カレンダーを見られる起点ユーザー（坂本達也）
DEFAULT_USER = "s-tatsuya2015@works-42585"

# シフトの入っているカレンダー（取得元）
SHIFT_CALENDARS = [
    {"calendar_id": "b6cc3c42-23e0-462c-a5e7-ca3c272f12bc"},  # 合同会社NeeSa
    {"calendar_id": "dfe29717-15f2-4fce-92b7-2b4baa37f4a2"},  # AceCosme 発送メンバー
]

# ディアメント/@121専用カレンダー（@121 spa&mart）。9-17形式でない特殊フォーマットのため
# SHIFT_CALENDARS/parse_shiftとは別に、専用パーサ(parse_at121)で読む。
AT121_CALENDAR_ID = "c_400183117_8750460c-3816-4fdd-88f9-169e3e1d57b9"

# 名前 → (会社, 部署) の個別マッピング（基本所属）。未登録は DEFAULT_GROUP。
# ※ シフト名の語尾に「@121」が付くと、その日はこのマッピングを上書きして
#   ディアメント/@121 へ移動する（AT121_GROUP, parse_shiftで検出）。
DEPT_MAP = {
    "梅津": ("NeeSa", "総務部"),
    "河野": ("NeeSa", "総務部"),
    "須賀": ("NeeSa", "SNS部"),
    "河村": ("NeeSa", "SNS部"),
    "三鹿": ("NeeSa", "WEB部"),
    "濵口": ("NeeSa", "WEB部"),
    "藤井": ("NeeSa", "WEB部"),
    "森岡": ("NeeSa", "WEB部"),  # トレコレKoTから取得
    "兼田": ("NeeSa", "総務部"),
    "佐藤": ("NeeSa", "商品管理部"),
    "矢垰": ("NeeSa", "商品管理部"),
    "杉村": ("NeeSa", "商品管理部"),
    "福田": ("NeeSa", "商品管理部"),
    "花園": ("NeeSa", "商品管理部"),
    "花園みどり": ("NeeSa", "商品管理部"),
    # 発送（既知メンバー。未登録者は既定で発送＋未マッピング通知）
    "加藤": ("NeeSa", "発送部"),
    "奥西": ("NeeSa", "発送部"),
    "岩本": ("NeeSa", "発送部"),
    "田邊": ("NeeSa", "発送部"),
    "大井": ("NeeSa", "発送部"),
    "石光": ("NeeSa", "発送部"),
    "井沢": ("NeeSa", "発送部"),
    "西村": ("NeeSa", "発送部"),
    "矢野": ("NeeSa", "発送部"),
    "山藤": ("アソビバスターズ", "商品管理部"),
    "宮崎": ("アソビバスターズ", "商品管理部"),  # トレコレKoTから取得
    # @121専属メンバー(2026-08-04): 他部署から@121へ異動
    "歩": ("ディアメント", "@121"),
    "田中": ("ディアメント", "@121"),
    "久保田": ("ディアメント", "@121"),
    "松田": ("ディアメント", "@121"),
    "平田": ("ディアメント", "@121"),
}
DEFAULT_GROUP = ("NeeSa", "発送部")
AT121_GROUP = ("ディアメント", "@121")  # 「@121」マーカー付きシフトの行き先
DEPT_ORDER = ["総務部", "発送部", "商品管理部", "SNS部", "WEB部", "@121"]
COMPANY_ORDER = ["NeeSa", "アソビバスターズ", "ディアメント"]
# 対象者ゼロでも枠を常時表示する(会社, 部署)
ALWAYS_SHOW = [("ディアメント", "@121")]
REMOTE_NAMES = {"梅津", "須賀", "三鹿"}  # リモート勤務者
# ボードに出さない人（退職・別管理・表示不要など）
EXCLUDE_NAMES = {"藤原", "佐々木", "有重", "伊藤", "曽我部", "坂本"}
# @121異動後、通常のSHIFT_CALENDARS側に残る旧ローテーション予定を無視し、
# @121 spa&martカレンダー側のみを正とする人(2026-08-05)
AT121_ONLY_NAMES = {"松田"}
# パート/契約社員(自分で退勤時刻を決めるため、時間指定なし出勤予定への
# 「打刻+9時間」自動表示の対象外とする人。それ以外は正社員(基本フレックス)扱い
PART_TIME_NAMES = {"須賀", "杉村", "花園", "平田", "石光", "梅津"}

# カレンダー表示名 → KoTフルネーム。旧姓等で姓がKoT登録名と一致しない人を、
# フルネーム指定で打刻に紐付ける（例: カレンダー「佐藤」＝KoT「佐々木果歩」(旧姓)）。
KOT_NAME_ALIAS = {"佐藤": "佐々木果歩"}
# 同姓の曖昧さ回避: カレンダー名(lastName) → 採用するKoTフルネーム
# 「大井」はスケジュールを持つ大井夏美を指す。同姓の大井蒼太はスケジュールが
# 無いため下の KOT_EXTRA_FULLNAMES で「蒼太」として打刻時のみ別表示する。
KOT_FULLNAME = {"大井": "大井夏美", "松田": "松田愛"}  # 松田: NeeSa総務部と@121兼任、KoT同姓(松田愛/松田唯)を松田愛に確定
# KoTフルネーム → (表示名, (会社, 部署))。姓だけだと同姓と衝突する人を、
# KoT打刻があった日のみ別表示名で出す（例: 大井蒼太 → 「蒼太」を発送へ）。
KOT_EXTRA_FULLNAMES = {"大井蒼太": ("蒼太", ("NeeSa", "発送部"))}
# 打刻が特殊な人: KoT打刻でなくLINEスケジュールの時間で状態判定
# （開始前=予定 / 時間内=出勤中 / 終了後=退勤済）
SCHEDULE_BASED_NAMES = {"山藤", "三鹿", "歩"}  # 歩=坂本歩(役員・KoTアカウントなし)
# NeeSa KoTではなくトレコレKoT(既存KOT_TOKEN)で打刻する人 → 当日打刻で表示
CROSS_KOT_NAMES = {"宮崎", "河村", "森岡"}
# トレコレKoT側で同姓が複数いる人の確定: 名字 → 採用するフルネーム
# （例: 河村は彩佳・遥華の2名がトレコレKoTに居るため彩佳に限定）
CROSS_KOT_FULLNAME = {"河村": "河村彩佳"}

_token_cache = {"access_token": None, "expires_at": 0}


def _create_jwt():
    now = int(time.time())
    payload = {"iss": CLIENT_ID, "sub": SERVICE_ACCOUNT, "iat": now, "exp": now + 3600}
    with open(PRIVATE_KEY_PATH, "r") as f:
        private_key = f.read()
    return jwt.encode(payload, private_key, algorithm="RS256")


def get_access_token():
    now = int(time.time())
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]
    data = {
        "assertion": _create_jwt(),
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "calendar.read",
    }
    try:
        resp = requests.post(AUTH_URL, data=data, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        _token_cache["access_token"] = result["access_token"]
        _token_cache["expires_at"] = now + int(result.get("expires_in", 3600))
        return result["access_token"]
    except Exception as e:
        logger.error("NeeSa LWトークン取得失敗: %s", e)
        return None


def get_calendar_events(calendar_id, from_dt, until_dt, user_id=DEFAULT_USER):
    """指定カレンダーの期間内イベント（eventComponentの平坦リスト）を返す"""
    token = get_access_token()
    if not token:
        return []
    url = f"{API_BASE}/users/{user_id}/calendars/{calendar_id}/events"
    headers = {"Authorization": "Bearer " + token}
    params = {"fromDateTime": from_dt, "untilDateTime": until_dt}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        comps = []
        for e in r.json().get("events", []):
            comps += e.get("eventComponents", [])
        return comps
    except Exception as e:
        logger.error("NeeSaカレンダー取得失敗 (%s): %s", calendar_id, e)
        return []


# 例: "9-14 兼田" / "11:00-21:00 内田" / "930-1730山藤" → ("兼田", "9", "14")
_SHIFT_RE = re.compile(
    r'^(\d{1,2}(?::?\d{2})?)\s*[\-〜~]\s*(\d{1,2}(?::?\d{2})?)\s*(.+)$'
)


def _fmt_time(t):
    """'1530'->'15:30', '9'->'9:00', '930'->'9:30', '9:30'->'9:30'。
    数字以外はそのまま返す。"""
    if not t:
        return t
    s = t.replace(":", "")
    if not s.isdigit():
        return t
    if len(s) <= 2:
        h, m = int(s), 0
    elif len(s) == 3:
        h, m = int(s[0]), int(s[1:])
    else:
        h, m = int(s[:2]), int(s[2:])
    return "%d:%02d" % (h, m)


def parse_shift(summary):
    """シフトsummaryを解析。時間レンジ形式のみシフトとみなす。
    返り値: dict(name, start, end) / 非シフト(休み・有給等)は None"""
    if not summary:
        return None
    m = _SHIFT_RE.match(summary.strip())
    if not m:
        return None
    start, end, name = _fmt_time(m.group(1)), _fmt_time(m.group(2)), m.group(3).strip()
    # 「@121」「＠121」マーカー（語尾等）を検出して名前から除去
    at121 = ("@121" in name) or ("＠121" in name)
    if at121:
        name = name.replace("@121", "").replace("＠121", "").strip()
    return {"name": name, "start": start, "end": end, "at121": at121}


def _applies_on(comp, target_date):
    """イベントコンポーネントが target_date に該当するか。
    繰り返し(RRULE)はEXDATEを考慮して展開判定。単発は開始日が一致するか。"""
    start = comp.get("start", {})
    sdt = start.get("dateTime")
    if not sdt:
        return False  # 終日/休/有給など時刻なしは対象外
    try:
        dtstart = isoparse(sdt).replace(tzinfo=None)
    except (ValueError, TypeError):
        return False

    rec = comp.get("recurrence")
    if not rec:
        return dtstart.date() == target_date

    rset = _rrule.rruleset()
    has_rule = False
    for line in rec:
        if line.startswith("RRULE:"):
            try:
                rset.rrule(_rrule.rrulestr(line[6:], dtstart=dtstart))
                has_rule = True
            except (ValueError, TypeError):
                pass
        elif line.startswith("EXDATE"):
            val = line.split(":", 1)[-1]
            for d in val.split(","):
                try:
                    rset.exdate(datetime.strptime(d.strip(), "%Y%m%dT%H%M%S"))
                except ValueError:
                    pass
    if not has_rule:
        return dtstart.date() == target_date
    day0 = datetime(target_date.year, target_date.month, target_date.day)
    for occ in rset.between(day0, day0 + timedelta(days=1), inc=True):
        if occ.date() == target_date:
            return True
    return False


# @121カレンダー: シフトでない行を除外する語
_AT121_EXCLUDE_RE = re.compile(r"定休日|有給|休み|オープニング|イベント|🎉")
# @121カレンダー: 人名に付く注記・兼務表記（長い順に）
_AT121_NOISE = ["5階兼任", "5階兼", "兼任", "出勤可", "5階"]


def _clean_at121_name(text):
    """@121のsummaryから人名だけ抽出。全角/半角スペース以降の注記を落とし付帯語を除去。"""
    if not text:
        return ""
    name = text.replace("　", " ").strip()
    name = name.split(" ")[0]
    for noise in _AT121_NOISE:
        name = name.replace(noise, "")
    return name.strip()


def parse_at121(comp):
    """@121イベントを解析。返り値 dict(name,start,end) / 対象外はNone。
    時刻: テキスト時間(930-1330) ＞ イベントのdateTime ＞ なし(終日)。"""
    summary = (comp.get("summary") or "").strip()
    if not summary or _AT121_EXCLUDE_RE.search(summary):
        return None
    m = _SHIFT_RE.match(summary)
    if m:  # 例 930-1330久保田 / 9-1645松田
        s_time, e_time = _fmt_time(m.group(1)), _fmt_time(m.group(2))
        name = _clean_at121_name(m.group(3))
    else:
        name = _clean_at121_name(summary)
        start, end = comp.get("start", {}) or {}, comp.get("end", {}) or {}
        if start.get("dateTime"):
            try:
                sd = isoparse(start["dateTime"]); s_time = "%d:%02d" % (sd.hour, sd.minute)
            except (ValueError, TypeError):
                s_time = None
            try:
                ed = isoparse(end["dateTime"]) if end.get("dateTime") else None
                e_time = "%d:%02d" % (ed.hour, ed.minute) if ed else None
            except (ValueError, TypeError):
                e_time = None
        else:
            s_time = e_time = None  # 終日
    if not name:
        return None
    return {"name": name, "start": s_time, "end": e_time}


def _applies_on_at121(comp, target_date):
    """@121用の該当判定。時刻あり(dateTime)は _applies_on を流用、
    終日(date)は 開始日<=target<終了日。繰り返し終日はRRULE展開。"""
    start = comp.get("start", {}) or {}
    if start.get("dateTime"):
        return _applies_on(comp, target_date)
    sd = start.get("date")
    if not sd:
        return False
    try:
        d0 = datetime.strptime(sd, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    ed = (comp.get("end", {}) or {}).get("date")
    try:
        d1 = datetime.strptime(ed, "%Y-%m-%d").date() if ed else d0 + timedelta(days=1)
    except (ValueError, TypeError):
        d1 = d0 + timedelta(days=1)
    rec = comp.get("recurrence")
    if rec:
        dtstart = datetime(d0.year, d0.month, d0.day)
        rset = _rrule.rruleset(); has_rule = False
        for line in rec:
            if line.startswith("RRULE:"):
                try:
                    rset.rrule(_rrule.rrulestr(line[6:], dtstart=dtstart)); has_rule = True
                except (ValueError, TypeError):
                    pass
            elif line.startswith("EXDATE"):
                for d in line.split(":", 1)[-1].split(","):
                    try:
                        rset.exdate(datetime.strptime(d.strip().split("T")[0], "%Y%m%d"))
                    except ValueError:
                        pass
        if has_rule:
            day0 = datetime(target_date.year, target_date.month, target_date.day)
            return any(o.date() == target_date
                       for o in rset.between(day0, day0 + timedelta(days=1), inc=True))
    return d0 <= target_date < d1


def get_names_by_date_range(start_date, end_date):
    """start_date〜end_date(両端含む、date型)の日ごとの出勤者名一覧を返す。
    {date_iso: [name, ...]}。カレンダー取得はSHIFT_CALENDARS分+@121の計数回のみ
    (日数分ではない)。/my/scheduleの月カレンダー表示で使用。"""
    base = datetime(start_date.year, start_date.month, start_date.day, tzinfo=JST)
    frm = base.isoformat()
    end_base = datetime(end_date.year, end_date.month, end_date.day, tzinfo=JST)
    unt = (end_base + timedelta(days=1)).isoformat()

    normal_comps = []
    for cal in SHIFT_CALENDARS:
        normal_comps.extend(get_calendar_events(cal["calendar_id"], frm, unt))
    at121_comps = get_calendar_events(AT121_CALENDAR_ID, frm, unt)

    result = {}
    d = start_date
    while d <= end_date:
        names = set()
        for c in normal_comps:
            parsed = parse_shift(c.get("summary", ""))
            if not parsed or parsed["name"] in EXCLUDE_NAMES or parsed["name"] in AT121_ONLY_NAMES:
                continue
            if _applies_on(c, d):
                names.add(parsed["name"])
        for c in at121_comps:
            p = parse_at121(c)
            if not p or p["name"] in EXCLUDE_NAMES:
                continue
            if _applies_on_at121(c, d):
                names.add(p["name"])
        result[d.isoformat()] = sorted(names)
        d += timedelta(days=1)
    return result


def get_shift_labels_by_date_range(start_date, end_date):
    """月カレンダーのマス目に直接表示するための、日毎の予定ラベル一覧を返す。
    {date_iso: [{"text": summary, "is_leave": bool}, ...]}。
    「9-18福田」のような通常シフトだけでなく「有給 井沢」等の非シフト予定も
    (時間レンジ形式でなくとも)summaryそのままで拾う点がget_names_by_date_rangeと異なる。"""
    base = datetime(start_date.year, start_date.month, start_date.day, tzinfo=JST)
    frm = base.isoformat()
    end_base = datetime(end_date.year, end_date.month, end_date.day, tzinfo=JST)
    unt = (end_base + timedelta(days=1)).isoformat()

    normal_comps = []
    for cal in SHIFT_CALENDARS:
        normal_comps.extend(get_calendar_events(cal["calendar_id"], frm, unt))
    at121_comps = get_calendar_events(AT121_CALENDAR_ID, frm, unt)

    result = {}
    d = start_date
    while d <= end_date:
        items = []
        for c in normal_comps:
            summary = (c.get("summary") or "").strip()
            if not summary:
                continue
            if any(n in summary for n in EXCLUDE_NAMES) or any(n in summary for n in AT121_ONLY_NAMES):
                continue
            if _applies_on(c, d):
                items.append({"text": summary, "is_leave": "有給" in summary})
        for c in at121_comps:
            summary = (c.get("summary") or "").strip()
            if not summary or any(n in summary for n in EXCLUDE_NAMES):
                continue
            if _applies_on_at121(c, d):
                items.append({"text": summary, "is_leave": "有給" in summary})
        result[d.isoformat()] = items
        d += timedelta(days=1)
    return result


def _fetch_normal_and_at121(frm, unt):
    """SHIFT_CALENDARS全部+@121を一括取得。[(comp, force_group), ...] と @121のcomp一覧を返す。
    複数日分をまとめて1回で取得するための下請け(get_today_shifts/範囲版で共用)。"""
    normal_with_group = []
    for cal in SHIFT_CALENDARS:
        for c in get_calendar_events(cal["calendar_id"], frm, unt):
            normal_with_group.append((c, cal.get("force_group")))
    at121_comps = get_calendar_events(AT121_CALENDAR_ID, frm, unt)
    return normal_with_group, at121_comps


def _group_shifts_for_day(normal_with_group, at121_comps, target_date):
    """事前取得済みのイベント一覧から、target_date一日分の会社/部署グループを組み立てる
    (get_today_shiftsの本体ロジック。複数日分をループする際に取得を使い回すため分離)。"""
    seen = {}
    for c, force_group in normal_with_group:
        parsed = parse_shift(c.get("summary", ""))
        if not parsed:
            continue
        if not _applies_on(c, target_date):
            continue
        if parsed["name"] in EXCLUDE_NAMES or parsed["name"] in AT121_ONLY_NAMES:
            continue
        parsed["summary"] = c.get("summary", "")
        parsed["remote"] = parsed["name"] in REMOTE_NAMES
        parsed["status"] = "scheduled"
        if force_group:
            parsed["force_group"] = force_group
        seen.setdefault(parsed["name"], parsed)

    at121_seen = {}
    for c in at121_comps:
        p = parse_at121(c)
        if not p or not _applies_on_at121(c, target_date):
            continue
        if p["name"] in EXCLUDE_NAMES:
            continue
        p["summary"] = c.get("summary", "")
        p["remote"] = p["name"] in REMOTE_NAMES
        p["status"] = "scheduled"
        p["unmapped"] = False
        at121_seen.setdefault(p["name"], p)

    grouped = {}
    for name, s in seen.items():
        if name in at121_seen:
            continue
        if s.get("force_group"):
            key = s["force_group"]
            s["unmapped"] = False
        elif s.get("at121"):
            key = AT121_GROUP
            s["unmapped"] = False
        elif name in DEPT_MAP:
            key = DEPT_MAP[name]
            s["unmapped"] = False
        else:
            key = DEFAULT_GROUP
            s["unmapped"] = True
        grouped.setdefault(key, []).append(s)
    for name, s in at121_seen.items():
        grouped.setdefault(AT121_GROUP, []).append(s)
    for cd in ALWAYS_SHOW:
        grouped.setdefault(cd, [])

    def _sort_key(item):
        (company, dept), _ = item
        ci = COMPANY_ORDER.index(company) if company in COMPANY_ORDER else 99
        di = DEPT_ORDER.index(dept) if dept in DEPT_ORDER else 99
        return (ci, di, dept)

    groups = []
    for (company, dept), shifts in sorted(grouped.items(), key=_sort_key):
        groups.append({
            "company": company,
            "dept": dept,
            "shifts": sorted(shifts, key=lambda s: (s.get("start") is None, s.get("start") or "")),
        })
    return groups


def get_today_shifts(target_date=None):
    """全シフトカレンダーから当日のシフトを取得し、名前→部署マッピングで
    会社/部署ごとにまとめる。繰り返しはRRULE展開で当日分のみ・名前で重複排除。
    返り値: [{company, dept, shifts:[{name,start,end,summary,remote,status}]}]"""
    if target_date is None:
        target_date = datetime.now(JST).date()
    base = datetime(target_date.year, target_date.month, target_date.day, tzinfo=JST)
    frm = base.isoformat()
    unt = (base + timedelta(days=1)).isoformat()
    normal_with_group, at121_comps = _fetch_normal_and_at121(frm, unt)
    return _group_shifts_for_day(normal_with_group, at121_comps, target_date)


def get_shifts_grouped_by_date_range(start_date, end_date):
    """get_today_shiftsの複数日版。カレンダー取得はSHIFT_CALENDARS分+@121の計数回のみ
    (日数分の取得を行わない)。返り値: {date_iso: [{company, dept, shifts:[...]}, ...]}
    /shiftsの週・月表示で日数分のAPI往復が発生し重くなっていたのを解消するために追加。"""
    base = datetime(start_date.year, start_date.month, start_date.day, tzinfo=JST)
    frm = base.isoformat()
    end_base = datetime(end_date.year, end_date.month, end_date.day, tzinfo=JST)
    unt = (end_base + timedelta(days=1)).isoformat()
    normal_with_group, at121_comps = _fetch_normal_and_at121(frm, unt)

    result = {}
    d = start_date
    while d <= end_date:
        result[d.isoformat()] = _group_shifts_for_day(normal_with_group, at121_comps, d)
        d += timedelta(days=1)
    return result


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    data = get_today_shifts()
    for g in data:
        print(f"\n■ {g['company']} / {g['dept']}  ({len(g['shifts'])}名)")
        for s in g["shifts"]:
            print(f"    {s['start']}-{s['end']}  {s['name']}")
