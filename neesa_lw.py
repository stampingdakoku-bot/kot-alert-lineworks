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
    # 将来: ディアメント/@121（NeeSaより出向）はカレンダー開始時に追加
]

# 名前 → (会社, 部署) の個別マッピング（基本所属）。未登録は DEFAULT_GROUP。
# ※ シフト名の語尾に「@121」が付くと、その日はこのマッピングを上書きして
#   ディアメント/@121 へ移動する（AT121_GROUP, parse_shiftで検出）。
DEPT_MAP = {
    "梅津": ("NeeSa", "総務部"),
    "河野": ("NeeSa", "総務部"),
    "久保田": ("NeeSa", "SNS部"),
    "須賀": ("NeeSa", "SNS部"),
    "河村": ("NeeSa", "SNS部"),
    "三鹿": ("NeeSa", "WEB"),
    "濵口": ("NeeSa", "WEB"),
    "藤井": ("NeeSa", "WEB"),
    "兼田": ("NeeSa", "総務部"),
    "松田": ("NeeSa", "総務部"),
    "佐藤": ("NeeSa", "商品管理"),
    "矢垰": ("NeeSa", "商品管理"),
    "田中": ("NeeSa", "商品管理"),
    "杉村": ("NeeSa", "商品管理"),
    "福田": ("NeeSa", "商品管理"),
    "花園": ("NeeSa", "商品管理"),
    "花園みどり": ("NeeSa", "商品管理"),
    # 発送（既知メンバー。未登録者は既定で発送＋未マッピング通知）
    "加藤": ("NeeSa", "発送"),
    "奥西": ("NeeSa", "発送"),
    "岩本": ("NeeSa", "発送"),
    "田邊": ("NeeSa", "発送"),
    "大井": ("NeeSa", "発送"),
    "大井蒼汰": ("NeeSa", "発送"),
    "石光": ("NeeSa", "発送"),
    "井沢": ("NeeSa", "発送"),
    "工藤": ("NeeSa", "発送"),
    "西村": ("NeeSa", "発送"),
    "矢野": ("NeeSa", "発送"),
    "山藤": ("アソビバスターズ", "アソビバスターズ発送"),
    "宮崎": ("アソビバスターズ", "アソビバスターズ発送"),  # トレコレKoTから取得
}
DEFAULT_GROUP = ("NeeSa", "発送")
AT121_GROUP = ("ディアメント", "@121")  # 「@121」マーカー付きシフトの行き先
DEPT_ORDER = ["商品管理", "発送", "総務部", "SNS部", "WEB",
              "アソビバスターズ発送", "@121"]
COMPANY_ORDER = ["NeeSa", "アソビバスターズ", "ディアメント"]
# 対象者ゼロでも枠を常時表示する(会社, 部署)
ALWAYS_SHOW = [("ディアメント", "@121")]
REMOTE_NAMES = {"梅津", "須賀", "三鹿"}  # リモート勤務者
# ボードに出さない人（退職・別管理・表示不要など）
EXCLUDE_NAMES = {"藤原", "佐々木", "有重", "伊藤", "曽我部"}

# 同姓の曖昧さ回避: カレンダー名(lastName) → 採用するKoTフルネーム
KOT_FULLNAME = {"大井": "大井夏美"}
# 打刻が特殊な人: KoT打刻でなくLINEスケジュールの時間で状態判定
# （開始前=予定 / 時間内=出勤中 / 終了後=退勤済）
SCHEDULE_BASED_NAMES = {"山藤"}
# NeeSa KoTではなくトレコレKoT(既存KOT_TOKEN)で打刻する人 → 当日打刻で表示
CROSS_KOT_NAMES = {"宮崎"}

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


def parse_shift(summary):
    """シフトsummaryを解析。時間レンジ形式のみシフトとみなす。
    返り値: dict(name, start, end) / 非シフト(休み・有給等)は None"""
    if not summary:
        return None
    m = _SHIFT_RE.match(summary.strip())
    if not m:
        return None
    start, end, name = m.group(1), m.group(2), m.group(3).strip()
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


def get_today_shifts(target_date=None):
    """全シフトカレンダーから当日のシフトを取得し、名前→部署マッピングで
    会社/部署ごとにまとめる。繰り返しはRRULE展開で当日分のみ・名前で重複排除。
    返り値: [{company, dept, shifts:[{name,start,end,summary,remote,status}]}]"""
    if target_date is None:
        target_date = datetime.now(JST).date()
    base = datetime(target_date.year, target_date.month, target_date.day, tzinfo=JST)
    frm = base.isoformat()
    unt = (base + timedelta(days=1)).isoformat()

    # 全カレンダーからシフトを集約（名前で重複排除）
    seen = {}
    for cal in SHIFT_CALENDARS:
        for c in get_calendar_events(cal["calendar_id"], frm, unt):
            parsed = parse_shift(c.get("summary", ""))
            if not parsed:
                continue
            if not _applies_on(c, target_date):
                continue
            if parsed["name"] in EXCLUDE_NAMES:
                continue
            parsed["summary"] = c.get("summary", "")
            parsed["remote"] = parsed["name"] in REMOTE_NAMES
            parsed["status"] = "scheduled"
            seen.setdefault(parsed["name"], parsed)

    # 名前 → (会社, 部署) で振り分け。@121マーカー付きは上書きで@121へ。
    # DEPT_MAP未登録(既定の発送行き)は unmapped=True で通知対象にする。
    grouped = {}
    for name, s in seen.items():
        if s.get("at121"):
            key = AT121_GROUP
            s["unmapped"] = False
        elif name in DEPT_MAP:
            key = DEPT_MAP[name]
            s["unmapped"] = False
        else:
            key = DEFAULT_GROUP
            s["unmapped"] = True
        grouped.setdefault(key, []).append(s)
    # 対象ゼロでも常時表示する枠を確保
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
            "shifts": sorted(shifts, key=lambda s: s["start"]),
        })
    return groups


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    data = get_today_shifts()
    for g in data:
        print(f"\n■ {g['company']} / {g['dept']}  ({len(g['shifts'])}名)")
        for s in g["shifts"]:
            print(f"    {s['start']}-{s['end']}  {s['name']}")
