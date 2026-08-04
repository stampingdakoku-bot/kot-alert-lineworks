"""
社内全体の出勤状況統合。staff_directoryの各行を kot_tenant に応じて
kot_write.fetch_all_records(既存KOT_WRITE_TOKEN/NEESA_KOT_WRITE_TOKEN経由の読み取り)
で振り分け、共通の形にまとめる。既存のkot_api.py/neesa_kot.py/neesa_lw.pyは
一切変更せず、/ ・/board はそれぞれ元のロジックのまま動き続ける。
"""
import logging
from datetime import datetime, timezone, timedelta

import kot_write
from db_supabase import supabase

JST = timezone(timedelta(hours=9))
logger = logging.getLogger(__name__)


def get_unified_status(date_str=None):
    """staff_directory全員分の当日出勤状況をまとめて返す。
    戻り値: [{..staff_directory列.., status, clock_in, clock_out}]
    status: 'working'(出勤中) / 'done'(退勤済) / 'none'(未打刻) / 'unknown'(KoT紐付けなし)"""
    if date_str is None:
        date_str = datetime.now(JST).strftime("%Y-%m-%d")

    result = supabase.table("staff_directory").select("*").eq("is_active", True).execute()
    staff_rows = result.data

    by_tenant = {
        "main": kot_write.fetch_all_records("main", date_str),
        "neesa": kot_write.fetch_all_records("neesa", date_str),
    }

    out = []
    for s in staff_rows:
        key = s.get("kot_employee_key")
        records = by_tenant.get(s["kot_tenant"], {}).get(key, []) if key else []
        clock_in = next((r["time"] for r in records if r.get("code") == "1"), None)
        clock_out = next((r["time"] for r in records if r.get("code") == "2"), None)
        if not key:
            status = "unknown"
        elif clock_out:
            status = "done"
        elif clock_in:
            status = "working"
        else:
            status = "none"
        out.append({**s, "status": status, "clock_in": clock_in, "clock_out": clock_out})
    return out


def get_preferences(staff_id):
    result = supabase.table("overview_preferences").select("*").eq("staff_id", staff_id).limit(1).execute()
    return result.data[0] if result.data else {"staff_id": staff_id, "hidden_names": []}


def save_preferences(staff_id, hidden_names):
    supabase.table("overview_preferences").upsert({
        "staff_id": staff_id,
        "hidden_names": hidden_names,
        "updated_at": datetime.now().isoformat(),
    }).execute()
