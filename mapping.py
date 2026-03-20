"""
kot-alert: 従業員 LINE WORKS ID マッピング管理

使い方:
  python3 mapping.py list              マッピング一覧
  python3 mapping.py add KEY LW_ID     マッピング追加
  python3 mapping.py sync              KOTから従業員同期
  python3 mapping.py employees         同期済み従業員一覧
  python3 mapping.py auto              対話式マッピング設定
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import kot_api

def cmd_sync():
    print("KOTから従業員データを取得中...")
    if kot_api.is_api_blocked():
        print("API利用禁止時間帯です。後で再実行してください。")
        return
    employees = kot_api.get_employees()
    if not employees:
        print("従業員データ取得失敗")
        return
    count = 0
    for emp in employees:
        db.upsert_employee(emp)
        count += 1
        name = f"{emp.get('lastName','')} {emp.get('firstName','')}"
        code = emp.get('code', '')
        div = emp.get('divisionName', '')
        typ = emp.get('typeName', '')
        print(f"  {code}: {name} ({div} / {typ})")
    print(f"\n{count}名の従業員データを同期しました")

def cmd_employees():
    emps = db.get_all_employees()
    if not emps:
        print("従業員データなし。先に sync を実行してください。")
        return
    print(f"\n{'コード':<8} {'名前':<16} {'所属':<16} {'雇用区分':<10} {'KEY(先頭16文字)'}")
    print("-" * 80)
    for e in emps:
        code = e.get('employee_code', '')
        name = f"{e.get('last_name','')} {e.get('first_name','')}"
        div = e.get('division_name', '')
        typ = e.get('type_name', '')
        key = e.get('employee_key', '')[:16]
        print(f"  {code:<8} {name:<16} {div:<16} {typ:<10} {key}...")

def cmd_list():
    mappings = db.get_all_mappings()
    if not mappings:
        print("マッピングなし。auto で設定してください。")
        return
    print(f"\n{'コード':<8} {'名前':<16} {'LINE WORKS ID':<30} {'Employee KEY(先頭16)'}")
    print("-" * 90)
    for m in mappings:
        code = m.get('employee_code', '') or '-'
        name = f"{m.get('last_name','') or ''} {m.get('first_name','') or ''}"
        lw_id = m.get('lw_account_id', '')
        key = m.get('employee_key', '')[:16]
        print(f"  {code:<8} {name:<16} {lw_id:<30} {key}...")

def cmd_add(emp_key, lw_id):
    db.set_lw_mapping(emp_key, lw_id)
    print(f"マッピング追加: {emp_key[:16]}... -> {lw_id}")

def cmd_auto():
    emps = db.get_all_employees()
    if not emps:
        print("従業員データなし。先に sync を実行します...")
        cmd_sync()
        emps = db.get_all_employees()
        if not emps:
            print("従業員データ取得できず")
            return
    print("\n=== 従業員 LINE WORKS ID マッピング設定 ===")
    print("各従業員のLINE WORKSアカウントIDを入力してください。")
    print("スキップ: 空Enter / 終了: q\n")
    for e in emps:
        code = e.get('employee_code', '')
        name = f"{e.get('last_name','')} {e.get('first_name','')}"
        key = e.get('employee_key', '')
        existing = db.get_lw_account_id(key)
        if existing:
            prompt = f"  {code} {name} [現在: {existing}] -> "
        else:
            prompt = f"  {code} {name} -> "
        lw_id = input(prompt).strip()
        if lw_id.lower() == 'q':
            break
        if lw_id:
            db.set_lw_mapping(key, lw_id)
            print(f"    設定完了")
    print("\n現在のマッピング:")
    cmd_list()

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "sync":
        cmd_sync()
    elif cmd == "employees":
        cmd_employees()
    elif cmd == "list":
        cmd_list()
    elif cmd == "add" and len(sys.argv) >= 4:
        cmd_add(sys.argv[2], sys.argv[3])
    elif cmd == "auto":
        cmd_auto()
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
