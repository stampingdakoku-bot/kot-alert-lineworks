import os
from datetime import datetime, date, timezone, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
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

JST = timezone(timedelta(hours=9))

def _parse_iso(s):
    """Python 3.10互換のISO 8601パーサー（非標準マイクロ秒桁数に対応）"""
    import re
    # 小数秒を6桁に正規化 (.3657 → .365700, .75 → .750000 等)
    s = re.sub(r'(\.\d+)', lambda m: (m.group(1)[:7].ljust(7, '0')), s)
    return datetime.fromisoformat(s)

@app.template_filter('to_jst')
def to_jst_filter(s):
    """UTC日時文字列をJST表示用に変換 (例: '03/20 16:00')"""
    if not s:
        return ''
    try:
        dt = _parse_iso(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_jst = dt.astimezone(JST)
        return dt_jst.strftime('%m/%d %H:%M')
    except (ValueError, TypeError):
        return s[5:16].replace('-', '/').replace('T', ' ')

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


def _get_store_shifts_and_attendance(today_str):
    """店舗ごとのシフト情報と出退勤状況を取得"""
    import re
    import kot_api
    import lw_api
    import requests as req_lib

    stores_result = supabase.table('store_calendars') \
        .select('*') \
        .eq('is_active', True) \
        .order('store_name') \
        .execute()
    stores = stores_result.data

    # 従業員マスタ
    all_employees = supabase.table('employees') \
        .select('*, mappings(lw_account_id)') \
        .order('employee_code') \
        .execute()
    emp_by_key = {}
    name_map = {}       # 名前 → 従業員（単一）
    name_dups = {}      # 同姓 → [従業員リスト]
    for e in all_employees.data:
        emp_by_key[e['employee_key']] = e
        last_name = (e.get('last_name') or '').strip()
        first_name = (e.get('first_name') or '').strip()
        if last_name:
            name_dups.setdefault(last_name, []).append(e)
            if last_name not in name_map:
                name_map[last_name] = e
        # フルネーム（姓名）でもマッチできるように登録
        if last_name and first_name:
            full = last_name + first_name
            if full not in name_map:
                name_map[full] = e
            full_sp = last_name + ' ' + first_name
            if full_sp not in name_map:
                name_map[full_sp] = e

    # LINE WORKS token for calendar API
    token = lw_api.get_access_token()
    headers = {"Authorization": "Bearer " + token} if token else {}

    # KoT timerecords
    timerecords = {}
    if not kot_api.is_api_blocked():
        raw = kot_api.get_timerecords(today_str)
        timerecords = kot_api.parse_timerecords_for_employee(raw)

    store_cards = []
    for store in stores:
        card = {
            'store_name': store['store_name'],
            'closing_hour': store['closing_hour'],
            'staff_scheduled': [],
            'staff_clocked_in': [],
            'staff_not_clocked': [],
            'calendar_error': False,
            'kot_blocked': kot_api.is_api_blocked(),
        }

        if not token:
            card['calendar_error'] = True
            store_cards.append(card)
            continue

        # Fetch calendar events
        uid = store.get('user_for_api', '')
        cid = store.get('calendar_id', '')
        from_dt = today_str + "T00:00:00+09:00"
        until_dt = today_str + "T23:59:59+09:00"
        url = (
            "https://www.worksapis.com/v1.0/users/" + uid
            + "/calendars/" + cid
            + "/events?fromDateTime=" + from_dt.replace("+", "%2B")
            + "&untilDateTime=" + until_dt.replace("+", "%2B")
            + "&count=100"
        )

        events = []
        try:
            r = req_lib.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                events = r.json().get("events", [])
            else:
                card['calendar_error'] = True
        except Exception:
            card['calendar_error'] = True

        # Parse shift names from events
        for event in events:
            components = event.get("eventComponents", [])
            if not components:
                continue
            comp = components[0]
            summary = comp.get("summary", "")

            # Parse name from summary like "13-22 内田" / "11:00-21:00 内田"
            shift_name = None
            m = re.match(r'^\d{1,2}(?::\d{2})?\s*[\-〜~]\s*\d{1,2}(?::\d{2})?\s*(.+)$', summary.strip())
            if m:
                shift_name = m.group(1).strip()

            if not shift_name:
                continue

            # Parse shift times
            start_info = comp.get("start", {})
            end_info = comp.get("end", {})
            start_dt_str = start_info.get("dateTime", "")
            end_dt_str = end_info.get("dateTime", "")
            shift_time = ""
            shift_start_dt = None
            if start_dt_str and end_dt_str:
                try:
                    s = datetime.fromisoformat(start_dt_str)
                    if s.tzinfo is None:
                        s = s.replace(tzinfo=JST)
                    e = datetime.fromisoformat(end_dt_str)
                    if e.tzinfo is None:
                        e = e.replace(tzinfo=JST)
                    shift_time = f"{s.hour}:{s.minute:02d}-{e.hour}:{e.minute:02d}"
                    # 繰り返しイベントは過去日付が返るため、今日の日付+時刻で再構築
                    today_date = date.fromisoformat(today_str)
                    shift_start_dt = datetime(today_date.year, today_date.month, today_date.day,
                                              s.hour, s.minute, 0, tzinfo=JST)
                except (ValueError, TypeError):
                    pass

            # 同姓が複数いる場合、KoTのdivisionNameで店舗に合致する従業員を選択
            emp = name_map.get(shift_name)
            candidates = name_dups.get(shift_name, [])
            if len(candidates) > 1:
                store_name = store['store_name']
                for c in candidates:
                    ck = c['employee_key']
                    if ck in timerecords:
                        recs = timerecords[ck].get('records', [])
                        if recs and store_name in recs[0].get('divisionName', ''):
                            emp = c
                            break
            emp_key = emp['employee_key'] if emp else None
            emp_code = emp.get('employee_code', '') if emp else ''
            full_name = shift_name

            # ç¹°ãè¿ãã¤ãã³ãã§dateTimeãåããªãå ´åãsummaryããæå»ãè§£æ
            if shift_start_dt is None:
                time_m = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*[\-ã~]', summary.strip())
                if time_m:
                    try:
                        h = int(time_m.group(1))
                        m_val = int(time_m.group(2)) if time_m.group(2) else 0
                        today_date = date.fromisoformat(today_str)
                        shift_start_dt = datetime(today_date.year, today_date.month, today_date.day,
                                                  h, m_val, 0, tzinfo=JST)
                    except (ValueError, TypeError):
                        pass

            now = datetime.now(JST)
            before_shift = bool(shift_start_dt and now < shift_start_dt)

            staff_info = {
                'name': full_name,
                'code': emp_code,
                'shift_time': shift_time,
                'emp_key': emp_key,
                'before_shift': before_shift,
            }

            card['staff_scheduled'].append(staff_info)

            # Check attendance from KoT (divisionNameで当該店舗にフィルタ)
            if emp_key and emp_key in timerecords:
                tr = timerecords[emp_key]
                store_name = store['store_name']
                all_recs = tr.get('records', [])
                store_recs = [r for r in all_recs
                              if store_name in r.get('divisionName', '')]
                # フィルタ後0件なら全打刻にフォールバック
                use_recs = store_recs if store_recs else all_recs
                # timeRecordのcode/timeからclock_in/clock_outを再計算
                store_clock_in = None
                store_clock_out = None
                for rec in use_recs:
                    code = str(rec.get('code', ''))
                    time_str = rec.get('time', '')
                    if not time_str:
                        continue
                    try:
                        t = _parse_iso(time_str)
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=JST)
                    except (ValueError, TypeError):
                        continue
                    if code == '1' and (store_clock_in is None or t < store_clock_in):
                        store_clock_in = t
                    elif code == '2' and (store_clock_out is None or t > store_clock_out):
                        store_clock_out = t
                if store_clock_in:
                    clock_in_str = store_clock_in.astimezone(JST).strftime('%H:%M')
                    clock_out_str = store_clock_out.astimezone(JST).strftime('%H:%M') if store_clock_out else None
                    card['staff_clocked_in'].append({
                        **staff_info,
                        'clock_in': clock_in_str,
                        'clock_out': clock_out_str,
                    })
                elif not before_shift:
                    card['staff_not_clocked'].append(staff_info)
            elif emp_key and not before_shift:
                card['staff_not_clocked'].append(staff_info)

        store_cards.append(card)

    return store_cards, all_employees.data


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

    # Store cards with shift/attendance data
    try:
        store_cards, all_emp_data = _get_store_shifts_and_attendance(today)
    except Exception as e:
        store_cards = []
        all_emp_data = supabase.table('employees') \
            .select('*, mappings(lw_account_id)') \
            .order('employee_code') \
            .execute().data

    # Unmapped staff count (exclude is_excluded=true)
    unmapped = [e for e in all_emp_data
                if not e.get('mappings') and not e.get('is_excluded')]
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

    return render_template('dashboard.html',
                           today=today,
                           summary=summary,
                           total_today=len(alerts_today.data),
                           recent=recent.data,
                           unmapped_count=unmapped_count,
                           unmapped_names=unmapped[:5],
                           problem_late=list(problem_late.values()),
                           problem_overtime=list(problem_overtime.values()),
                           store_cards=store_cards,
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


@app.route('/staff/<employee_key>/toggle_exclude', methods=['POST'])
def staff_toggle_exclude(employee_key):
    # Get current state
    result = supabase.table('employees') \
        .select('is_excluded') \
        .eq('employee_key', employee_key) \
        .limit(1) \
        .execute()
    if result.data:
        current = result.data[0].get('is_excluded', False)
        supabase.table('employees').update({
            'is_excluded': not current
        }).eq('employee_key', employee_key).execute()
        status = '除外しました' if not current else '除外を解除しました'
        flash(status, 'success')
    return redirect(url_for('staff_list'))


# --- Logs ---
@app.route('/logs')
def logs():
    flow_type = request.args.get('flow_type', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    show_unapplied = request.args.get('unapplied', '')

    unapplied_list = []
    if show_unapplied:
        # 未申請者抽出: 乖離通知ありだが申請リマインドが0件のスタッフ
        target_date = show_unapplied
        dev_result = supabase.table('alerts_sent') \
            .select('employee_key, employees(employee_code, last_name, first_name)') \
            .eq('alert_date', target_date) \
            .eq('flow_type', 'deviation') \
            .execute()
        rem_result = supabase.table('alerts_sent') \
            .select('employee_key') \
            .eq('alert_date', target_date) \
            .eq('flow_type', 'request_reminder') \
            .execute()
        reminded_keys = {r['employee_key'] for r in rem_result.data}
        for d in dev_result.data:
            if d['employee_key'] not in reminded_keys:
                emp = d.get('employees') or {}
                unapplied_list.append({
                    'employee_key': d['employee_key'],
                    'code': emp.get('employee_code', ''),
                    'name': (emp.get('last_name', '') + ' ' + emp.get('first_name', '')).strip(),
                    'date': target_date,
                })

    query = supabase.table('alerts_sent') \
        .select('*, employees(last_name, first_name)') \
        .order('created_at', desc=True)

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
                           filter_to=date_to,
                           unapplied_list=unapplied_list,
                           show_unapplied=show_unapplied)


# --- Shifts ---
@app.route('/shifts')
def shifts():
    import re
    import lw_api
    import requests as req_lib

    stores_result = supabase.table('store_calendars') \
        .select('*') \
        .eq('is_active', True) \
        .order('store_name') \
        .execute()
    stores = stores_result.data

    # Build name map from employees
    all_employees = supabase.table('employees') \
        .select('employee_key, employee_code, last_name, first_name') \
        .order('employee_code') \
        .execute()
    name_map = {}
    for e in all_employees.data:
        last_name = (e.get('last_name') or '').strip()
        if last_name and last_name not in name_map:
            name_map[last_name] = e

    token = lw_api.get_access_token()
    headers = {"Authorization": "Bearer " + token} if token else {}

    today = date.today()
    dates = [(today + timedelta(days=i)) for i in range(7)]
    today_str = today.isoformat()
    end_date = dates[-1]

    # Default to first store tab
    active_store = request.args.get('store', stores[0]['store_name'] if stores else '')

    # Build store_shifts: only fetch calendar for the active store
    store_shifts = []
    for store in stores:
        if store['store_name'] != active_store:
            store_shifts.append({
                'store_name': store['store_name'],
                'closing_hour': store['closing_hour'],
                'days': [],
            })
            continue

        uid = store.get('user_for_api', '')
        cid = store.get('calendar_id', '')

        # Fetch events day-by-day (繰り返しイベント展開のため1日単位で取得)
        events_by_date = {}
        if token and uid and cid:
            for d in dates:
                d_str = d.isoformat()
                from_dt = d_str + "T00:00:00+09:00"
                until_dt = d_str + "T23:59:59+09:00"
                url = (
                    "https://www.worksapis.com/v1.0/users/" + uid
                    + "/calendars/" + cid
                    + "/events?fromDateTime=" + from_dt.replace("+", "%2B")
                    + "&untilDateTime=" + until_dt.replace("+", "%2B")
                    + "&count=100"
                )
                try:
                    r = req_lib.get(url, headers=headers, timeout=15)
                    if r.status_code != 200:
                        continue
                    day_events = r.json().get("events", [])
                except Exception:
                    continue

                for event in day_events:
                    components = event.get("eventComponents", [])
                    if not components:
                        continue
                    comp = components[0]
                    summary = comp.get("summary", "")

                    shift_name = None
                    m = re.match(r'^\d{1,2}(?::\d{2})?\s*[\-〜~]\s*\d{1,2}(?::\d{2})?\s*(.+)$', summary.strip())
                    if m:
                        shift_name = m.group(1).strip()

                    if not shift_name:
                        continue

                    start_info = comp.get("start", {})
                    end_info = comp.get("end", {})
                    start_dt_str = start_info.get("dateTime", "")
                    end_dt_str = end_info.get("dateTime", "")
                    start_time = ""
                    end_time = ""
                    if start_dt_str:
                        try:
                            s = datetime.fromisoformat(start_dt_str)
                            if s.tzinfo is None:
                                s = s.replace(tzinfo=JST)
                            start_time = f"{s.hour}:{s.minute:02d}"
                        except (ValueError, TypeError):
                            pass
                    if end_dt_str:
                        try:
                            e = datetime.fromisoformat(end_dt_str)
                            if e.tzinfo is None:
                                e = e.replace(tzinfo=JST)
                            end_time = f"{e.hour}:{e.minute:02d}"
                        except (ValueError, TypeError):
                            pass

                    events_by_date.setdefault(d_str, []).append({
                        'name': shift_name,
                        'start': start_time,
                        'end': end_time,
                    })

        days = []
        for d in dates:
            d_str = d.isoformat()
            day_shifts = sorted(events_by_date.get(d_str, []), key=lambda x: x['start'])
            days.append({
                'date': d_str,
                'weekday': ['月', '火', '水', '木', '金', '土', '日'][d.weekday()],
                'is_today': d == today,
                'shifts': day_shifts,
            })

        store_shifts.append({
            'store_name': store['store_name'],
            'closing_hour': store['closing_hour'],
            'days': days,
        })

    return render_template('shifts.html',
                           store_shifts=store_shifts,
                           active_store=active_store,
                           today=today_str)


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


# --- Settings ---
@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        updates = {
            'clockin_alarm_enabled': 'clockin_alarm_enabled' in request.form,
            'late_clockin_enabled': 'late_clockin_enabled' in request.form,
            'late_clockin_start_minutes': int(request.form.get('late_clockin_start_minutes', 10)),
            'late_clockin_interval_minutes': int(request.form.get('late_clockin_interval_minutes', 10)),
            'late_clockin_max_count': int(request.form.get('late_clockin_max_count', 4)),
            'clockout_alarm_enabled': 'clockout_alarm_enabled' in request.form,
            'overtime_enabled': 'overtime_enabled' in request.form,
            'overtime_start_minutes': int(request.form.get('overtime_start_minutes', 10)),
            'overtime_interval_minutes': int(request.form.get('overtime_interval_minutes', 10)),
            'overtime_max_count': int(request.form.get('overtime_max_count', 4)),
            'deviation_enabled': 'deviation_enabled' in request.form,
            'request_reminder_enabled': 'request_reminder_enabled' in request.form,
            'request_reminder_interval_minutes': int(request.form.get('request_reminder_interval_minutes', 10)),
            'request_reminder_max_count': int(request.form.get('request_reminder_max_count', 2)),
            'admin_lw_id': request.form.get('admin_lw_id', '').strip(),
            'daily_summary_enabled': 'daily_summary_enabled' in request.form,
            'daily_summary_hour': int(request.form.get('daily_summary_hour', 23)),
            'daily_summary_minute': int(request.form.get('daily_summary_minute', 0)),
            'morning_check_enabled': 'morning_check_enabled' in request.form,
            'morning_check_hour': int(request.form.get('morning_check_hour', 10)),
            'morning_check_minute': int(request.form.get('morning_check_minute', 10)),
            'updated_at': datetime.now(JST).isoformat(),
        }
        supabase.table('alert_settings').update(updates).eq('id', 1).execute()
        flash('設定を保存しました', 'success')
        return redirect(url_for('settings'))

    try:
        result = supabase.table('alert_settings').select('*').eq('id', 1).execute()
        s = result.data[0] if result.data else {}
    except Exception:
        s = {}
        flash('alert_settingsテーブルが見つかりません。Supabase SQL Editorでテーブルを作成してください。', 'error')
    return render_template('settings.html', s=s)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
