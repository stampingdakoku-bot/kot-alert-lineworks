"""
staff_directory 同期: 本体KoT(トレコレ)社員 + NeeSa/ボード系スタッフ を
単一の staff_id で管理する統合スタッフ台帳を構築する。

- 本体系統(source扱いは省略・シンプル化): employees + mappings から自動同期
- NeeSa/ボード系統: neesa_lw.DEPT_MAP の氏名を NeeSa KoT 社員と突き合わせて解決
  (突き合わせロジックは neesa_kot._build_lastname_map と同じ KOT_FULLNAME ピンを使用)

このモジュールは既存の employees/mappings/DEPT_MAP を読むだけで書き換えない。
checker.py・/ ・/board は今まで通り既存データソースを直接参照し続ける(無変更)。
"""
import logging
from datetime import datetime
from dotenv import load_dotenv

import db_supabase as db
import neesa_lw
import neesa_kot
from db_supabase import supabase

logger = logging.getLogger(__name__)
load_dotenv()


def _upsert(rows):
    for r in rows:
        r["updated_at"] = datetime.now().isoformat()
    if rows:
        supabase.table("staff_directory").upsert(
            rows, on_conflict="kot_tenant,external_key"
        ).execute()


def sync_main_employees():
    """本体KoT(トレコレ店舗)社員をstaff_directoryへ同期。employee_key単位でupsert。"""
    employees = db.get_all_employees()
    mappings = {m["employee_key"]: m["lw_account_id"] for m in db.get_all_mappings()}
    rows = []
    for e in employees:
        if e.get("is_excluded"):
            continue
        key = e.get("employee_key")
        if not key:
            continue
        name = (e.get("last_name") or "") + (e.get("first_name") or "")
        rows.append({
            "external_key": key,
            "display_name": name or e.get("employee_code", ""),
            "company": "アソビバスターズ",
            "dept": e.get("division_name") or "",
            "kot_tenant": "main",
            "kot_employee_key": key,
            "lw_account_id": mappings.get(key),
            "is_active": True,
        })
    _upsert(rows)
    return len(rows)


def sync_neesa_staff():
    """NeeSa/ボード系(DEPT_MAP)スタッフをstaff_directoryへ同期。
    氏名(姓)をNeeSa KoT社員と突き合わせ、KOT_FULLNAMEピンで同姓を解決。"""
    emps = neesa_kot.get_employees() or []
    byln = {}
    for e in emps:
        ln = e.get("lastName", "")
        full = ln + e.get("firstName", "")
        pin = neesa_lw.KOT_FULLNAME.get(ln)
        if pin and full != pin:
            continue
        if ln not in byln:
            byln[ln] = e.get("key")

    rows = []
    for name, (company, dept) in neesa_lw.DEPT_MAP.items():
        if name in neesa_lw.EXCLUDE_NAMES:
            continue
        rows.append({
            "external_key": name,
            "display_name": name,
            "company": company,
            "dept": dept,
            "kot_tenant": "neesa",
            "kot_employee_key": byln.get(name),
            "lw_account_id": None,
            "is_active": True,
        })
    _upsert(rows)
    return len(rows)


def sync_all():
    n1 = sync_main_employees()
    n2 = sync_neesa_staff()
    logger.info("staff_directory同期完了: 本体%d件・NeeSa%d件", n1, n2)
    return n1, n2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    n1, n2 = sync_all()
    print(f"本体: {n1}件, NeeSa: {n2}件")
    result = supabase.table("staff_directory").select("*").order("company").execute()
    for r in result.data:
        print(f"  [{r['kot_tenant']}] {r['company']}/{r['dept']} {r['display_name']} "
              f"(kot_key={'あり' if r['kot_employee_key'] else 'なし'})")
