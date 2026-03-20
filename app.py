import os
from datetime import datetime, date
from flask import Flask, render_template, request, redirect, url_for, flash, session
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev')

ADMIN_PASSCODE = os.getenv('ADMIN_PASSCODE', '000000')

supabase = create_client(
    os.getenv('SUPABASE_URL'),
    os.getenv('SUPABASE_SERVICE_ROLE_KEY')
)

FLOW_LABELS = {
    'clockin_alarm': '出勤アラーム',
    'clockout_alarm': '退勤アラーム',
    'late_clockin': '出勤打刻なし',
    'overtime': '超過警告',
    'deviation': '乖離通知',
    'request_reminder': '申請リマインド',
    'morning_check': '翌朝チェック',
}


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('passcode') == ADMIN_PASSCODE:
            session['authenticated'] = True
            return redirect(url_for('dashboard'))
        flash('パスコードが正しくありません', 'error')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.before_request
def check_auth():
    if request.endpoint in ('login', 'static'):
        return
    if not session.get('authenticated'):
        return redirect(url_for('login'))


# --- Dashboard ---
@app.route('/')
def dashboard():
    today = date.today().isoformat()

    # Today's alerts with employee info
    alerts_today = supabase.table('alerts_sent') \
        .select('*, employees(employee_code, last_name, first_name)') \
        .eq('alert_date', today) \
        .order('created_at', desc=True) \
        .execute()

    # Summary by flow_type
    summary = {}
    for ft in ['clockin_alarm', 'clockout_alarm', 'late_clockin',
               'overtime', 'deviation', 'request_reminder', 'morning_check']:
        summary[ft] = 0
    for a in alerts_today.data:
        ft = a['flow_type']
        summary[ft] = summary.get(ft, 0) + 1

    # Recent alerts (last 20)
    recent = supabase.table('alerts_sent') \
        .select('*, employees(last_name, first_name)') \
        .order('created_at', desc=True) \
        .limit(20) \
        .execute()

    # Unmapped staff count
    all_employees = supabase.table('employees') \
        .select('employee_key, employee_code, last_name, first_name, mappings(lw_account_id)') \
        .order('employee_code') \
        .execute()
    unmapped = [e for e in all_employees.data if not e.get('mappings')]
    unmapped_count = len(unmapped)

    # Problem staff: late_clockin and overtime today
    problem_late = {}
    problem_overtime = {}
    for a in alerts_today.data:
        emp = a.get('employees') or {}
        name = (emp.get('last_name', '') + ' ' + emp.get('first_name', '')).strip()
        code = emp.get('employee_code', '')
        key = a.get('employee_key', '')
        if not name or key == '__admin__':
            continue
        if a['flow_type'] == 'late_clockin':
            if key not in problem_late:
                problem_late[key] = {'name': name, 'code': code, 'count': 0}
            problem_late[key]['count'] += 1
        elif a['flow_type'] == 'overtime':
            if key not in problem_overtime:
                problem_overtime[key] = {'name': name, 'code': code, 'count': 0}
            problem_overtime[key]['count'] += 1

    # Store calendars
    stores = supabase.table('store_calendars') \
        .select('store_name, closing_hour, is_active') \
        .eq('is_active', True) \
        .order('store_name') \
        .execute()

    return render_template('dashboard.html',
                           today=today,
                           summary=summary,
                           total_today=len(alerts_today.data),
                           recent=recent.data,
                           unmapped_count=unmapped_count,
                           unmapped_names=unmapped[:5],
                           problem_late=list(problem_late.values()),
                           problem_overtime=list(problem_overtime.values()),
                           stores=stores.data,
                           flow_labels=FLOW_LABELS)


# --- Staff ---
@app.route('/staff')
def staff_list():
    employees = supabase.table('employees') \
        .select('*, mappings(lw_account_id)') \
        .order('employee_code') \
        .execute()
    return render_template('staff.html', employees=employees.data)


@app.route('/staff/add', methods=['POST'])
def staff_add():
    data = {
        'employee_key': request.form['employee_key'],
        'employee_code': request.form['employee_code'],
        'last_name': request.form['last_name'],
        'first_name': request.form['first_name'],
    }
    supabase.table('employees').insert(data).execute()

    lw_account = request.form.get('lw_account_id', '').strip()
    if lw_account:
        supabase.table('mappings').insert({
            'employee_key': data['employee_key'],
            'lw_account_id': lw_account
        }).execute()

    flash('スタッフを追加しました', 'success')
    return redirect(url_for('staff_list'))


@app.route('/staff/<employee_key>/edit', methods=['POST'])
def staff_edit(employee_key):
    supabase.table('employees').update({
        'employee_code': request.form['employee_code'],
        'last_name': request.form['last_name'],
        'first_name': request.form['first_name'],
    }).eq('employee_key', employee_key).execute()

    lw_account = request.form.get('lw_account_id', '').strip()
    if lw_account:
        supabase.table('mappings').upsert({
            'employee_key': employee_key,
            'lw_account_id': lw_account,
            'updated_at': datetime.now().isoformat()
        }).execute()

    flash('スタッフ情報を更新しました', 'success')
    return redirect(url_for('staff_list'))


@app.route('/staff/<employee_key>/delete', methods=['POST'])
def staff_delete(employee_key):
    supabase.table('mappings').delete().eq('employee_key', employee_key).execute()
    supabase.table('employees').delete().eq('employee_key', employee_key).execute()
    flash('スタッフを削除しました', 'success')
    return redirect(url_for('staff_list'))


# --- Logs ---
@app.route('/logs')
def logs():
    query = supabase.table('alerts_sent') \
        .select('*, employees(last_name, first_name)') \
        .order('created_at', desc=True)

    flow_type = request.args.get('flow_type', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    if flow_type:
        query = query.eq('flow_type', flow_type)
    if date_from:
        query = query.gte('alert_date', date_from)
    if date_to:
        query = query.lte('alert_date', date_to)

    result = query.limit(100).execute()

    flow_types = [
        'clockin_alarm', 'clockout_alarm', 'late_clockin',
        'overtime', 'deviation', 'request_reminder', 'morning_check'
    ]

    return render_template('logs.html',
                           logs=result.data,
                           flow_types=flow_types,
                           filter_flow=flow_type,
                           filter_from=date_from,
                           filter_to=date_to)


# --- Stores ---
@app.route('/stores')
def stores():
    result = supabase.table('store_calendars') \
        .select('*') \
        .order('store_name') \
        .execute()
    return render_template('stores.html', stores=result.data)


@app.route('/stores/edit', methods=['POST'])
def store_edit():
    supabase.table('store_calendars').update({
        'calendar_id': request.form['calendar_id'],
        'user_for_api': request.form['user_for_api'],
        'closing_hour': int(request.form['closing_hour']),
        'is_active': 'is_active' in request.form,
    }).eq('store_name', request.form['store_name']).execute()

    flash('店舗設定を更新しました', 'success')
    return redirect(url_for('stores'))


@app.route('/stores/add', methods=['POST'])
def store_add():
    supabase.table('store_calendars').insert({
        'store_name': request.form['store_name'],
        'calendar_id': request.form['calendar_id'],
        'user_for_api': request.form['user_for_api'],
        'closing_hour': int(request.form['closing_hour']),
    }).execute()
    flash('店舗を追加しました', 'success')
    return redirect(url_for('stores'))


@app.route('/stores/<store_name>/delete', methods=['POST'])
def store_delete(store_name):
    supabase.table('store_calendars').delete().eq('store_name', store_name).execute()
    flash('店舗を削除しました', 'success')
    return redirect(url_for('stores'))


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
