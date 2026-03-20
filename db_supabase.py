"""
kot-alert: データベース管理 (Supabase版)
"""
import os
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(
    os.getenv('SUPABASE_URL'),
    os.getenv('SUPABASE_SERVICE_ROLE_KEY')
)


def was_alert_sent(employee_key, flow_type, alert_date):
    result = supabase.table('alerts_sent') \
        .select('id') \
        .eq('employee_key', employee_key) \
        .eq('flow_type', flow_type) \
        .eq('alert_date', alert_date) \
        .limit(1) \
        .execute()
    return len(result.data) > 0


def record_alert(employee_key, flow_type, alert_date, message=""):
    supabase.table('alerts_sent').insert({
        'employee_key': employee_key,
        'flow_type': flow_type,
        'alert_date': alert_date,
        'sent_at': datetime.now().isoformat(),
        'message': message
    }).execute()


def count_alerts_today(employee_key, flow_type, alert_date):
    result = supabase.table('alerts_sent') \
        .select('id', count='exact') \
        .eq('employee_key', employee_key) \
        .eq('flow_type', flow_type) \
        .eq('alert_date', alert_date) \
        .execute()
    return result.count if result.count is not None else len(result.data)


def get_reminder_tracking(employee_key, alert_date):
    result = supabase.table('reminder_tracking') \
        .select('*') \
        .eq('employee_key', employee_key) \
        .eq('alert_date', alert_date) \
        .limit(1) \
        .execute()
    return result.data[0] if result.data else None


def upsert_reminder(employee_key, alert_date):
    now = datetime.now().isoformat()
    existing = get_reminder_tracking(employee_key, alert_date)
    if existing:
        supabase.table('reminder_tracking').update({
            'last_reminded_at': now,
            'remind_count': existing['remind_count'] + 1
        }).eq('employee_key', employee_key) \
          .eq('alert_date', alert_date) \
          .execute()
    else:
        supabase.table('reminder_tracking').insert({
            'employee_key': employee_key,
            'alert_date': alert_date,
            'last_reminded_at': now,
            'remind_count': 1
        }).execute()


def mark_reminder_resolved(employee_key, alert_date):
    supabase.table('reminder_tracking').update({
        'resolved': True
    }).eq('employee_key', employee_key) \
      .eq('alert_date', alert_date) \
      .execute()


def upsert_employee(emp):
    supabase.table('employees').upsert({
        'employee_key': emp['key'],
        'employee_code': emp.get('code', ''),
        'last_name': emp.get('lastName', ''),
        'first_name': emp.get('firstName', ''),
        'division_code': emp.get('divisionCode', ''),
        'division_name': emp.get('divisionName', ''),
        'type_code': emp.get('typeCode', ''),
        'type_name': emp.get('typeName', ''),
        'updated_at': datetime.now().isoformat()
    }).execute()


def get_all_employees():
    result = supabase.table('employees').select('*').execute()
    return result.data


def set_lw_mapping(employee_key, lw_account_id):
    supabase.table('mappings').upsert({
        'employee_key': employee_key,
        'lw_account_id': lw_account_id,
        'updated_at': datetime.now().isoformat()
    }).execute()


def get_lw_account_id(employee_key):
    result = supabase.table('mappings') \
        .select('lw_account_id') \
        .eq('employee_key', employee_key) \
        .limit(1) \
        .execute()
    return result.data[0]['lw_account_id'] if result.data else None


def get_all_mappings():
    result = supabase.table('mappings') \
        .select('employee_key, lw_account_id, employees(employee_code, last_name, first_name)') \
        .execute()
    rows = []
    for m in result.data:
        emp = m.get('employees') or {}
        rows.append({
            'employee_key': m['employee_key'],
            'lw_account_id': m['lw_account_id'],
            'employee_code': emp.get('employee_code', ''),
            'last_name': emp.get('last_name', ''),
            'first_name': emp.get('first_name', ''),
        })
    return rows


def get_store_calendars():
    """store_calendarsテーブルから有効な店舗設定を取得"""
    result = supabase.table('store_calendars') \
        .select('*') \
        .eq('is_active', True) \
        .execute()
    stores = {}
    for s in result.data:
        stores[s['store_name']] = {
            'calendar_id': s['calendar_id'],
            'closing_hour': s['closing_hour'],
            'user_for_api': s['user_for_api'],
        }
    return stores
