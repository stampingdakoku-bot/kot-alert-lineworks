import os
from datetime import datetime, date, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev')

from my_portal import my_bp  # noqa: E402 (個人メニュー: 勤怠ボタン・出勤状況一覧・スケジュール登録)
app.register_blueprint(my_bp)

ADMIN_PASSCODE = os.getenv('ADMIN_PASSCODE', '000000')

supabase = create_client(
    os.getenv('SUPABASE_URL'),
    os.getenv('SUPABASE_SERVICE_ROLE_KEY')
)

JST = timezone(timedelta(hours=9))


@app.after_request
def _no_cache_html(response):
    """個人カラー等セッション依存の描画結果がブラウザ/戻る進むキャッシュで
    古いまま表示され続けるのを防ぐ(HTMLレスポンスのみ対象)"""
    if response.content_type and response.content_type.startswith('text/html'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.before_request
def _sync_bg_color_session():
    """ログイン中はDBから直接bg_colorを読み込みsessionに反映する。
    (以前は「sessionに無い時だけ補完」だったが、何らかの理由でsession側が古いまま
    更新されないケースがあり、マイメニュー以外のページに新しい色が反映されない
    不具合の原因となっていたため、最新化する方式に変更した。ただし毎リクエストDBに
    問い合わせると重くなるため、10秒間はキャッシュを使い回す)"""
    if session.get('staff_id'):
        now = datetime.now().timestamp()
        if now - session.get('bg_synced_at', 0) < 10:
            return
        try:
            result = supabase.table('staff_directory').select('bg_color') \
                .eq('staff_id', session['staff_id']).limit(1).execute()
            session['bg_color'] = result.data[0].get('bg_color') if result.data else None
            session['bg_synced_at'] = now
        except Exception:
            pass

# 異体字正規化マップ（KoT登録名 → カレンダー表記）
KANJI_VARIANTS = {
    '𠮷': '吉',  # 𠮷(土吉) → 吉
    '髙': '高',      # 髙 → 高
    '濱': '浜',      # 濱 → 浜
    '晃': '晴',      # 晃 → 晴 (not in use but common)
    '峵': '崎',      # 峵 → 崎
    '邉': '辺',      # 邉 → 辺
    '邊': '辺',      # 邊 → 辺
    '瀨': '瀬',      # 瀨 → 瀬
}

def _normalize_name(name):
    """異体字を通常の漢字に正規化"""
    return ''.join(KANJI_VARIANTS.get(c, c) for c in name)

def _parse_iso(s):
    """Python 3.10互換のISO 8601パーサー（非標準マイクロ秒桁数に対応）"""
    import re
    # 小数秒を6桁に正規化 (.3657 → .365700, .75 → .750000 等)
    s = re.sub(r'(\.\d+)', lambda m: (m.group(1)[:7].ljust(7, '0')), s)
    return datetime.fromisoformat(s)

import zlib

@app.template_filter('name_hue')
def name_hue_filter(name):
    """名前から決定論的に色相(0-359)を生成。アバターグループの色分けに使用"""
    if not name:
        return 210
    return zlib.crc32(name.encode('utf-8')) % 360

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
    # 一時的に認証を無効化
    return
    # if request.endpoint in ('login', 'static'):
    #     return
    # if not session.get('authenticated'):
    #     return redirect(url_for('login'))


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
            # 異体字正規化名でも登録
            norm_last = _normalize_name(last_name)
            if norm_last != last_name:
                name_dups.setdefault(norm_last, []).append(e)
                if norm_last not in name_map:
                    name_map[norm_last] = e
        # フルネーム（姓名）でもマッチできるように登録
        if last_name and first_name:
            full = last_name + first_name
            if full not in name_map:
                name_map[full] = e
            full_sp = last_name + ' ' + first_name
            if full_sp not in name_map:
                name_map[full_sp] = e
            # 正規化フルネーム
            norm_full = _normalize_name(last_name) + first_name
            if norm_full not in name_map:
                name_map[norm_full] = e
            norm_full_sp = _normalize_name(last_name) + ' ' + first_name
            if norm_full_sp not in name_map:
                name_map[norm_full_sp] = e

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
    active_tab = request.args.get('tab')
    if active_tab not in ('realtime', 'shift'):
        active_tab = 'realtime'
    today_actual = date.today().isoformat()
    max_future_date = (date.today() + timedelta(days=7))
    date_param = (request.args.get('date') or '').strip()
    try:
        if date_param:
            selected = date.fromisoformat(date_param)
        else:
            selected = date.today()
    except ValueError:
        selected = date.today()
    # 翌日以降のシフト確認のため、最大7日先までは表示可能にする
    if selected > max_future_date:
        selected = max_future_date
    max_future_date = max_future_date.isoformat()
    today = selected.isoformat()
    is_past = today < today_actual
    is_today = today == today_actual
    is_future = today > today_actual
    prev_date = (selected - timedelta(days=1)).isoformat()
    next_date = (selected + timedelta(days=1)).isoformat()

    # 選択日のアラート（従業員情報付き）
    alerts_today = supabase.table('alerts_sent') \
        .select('*, employees(employee_code, last_name, first_name, division_name)') \
        .eq('alert_date', today) \
        .order('created_at', desc=True) \
        .execute()

    # インフォメーション設定(マーキー)
    marquee_text = ''
    try:
        settings_row = supabase.table('alert_settings').select('dashboard_marquee_text').eq('id', 1).execute()
        if settings_row.data:
            marquee_text = settings_row.data[0].get('dashboard_marquee_text') or ''
    except Exception:
        marquee_text = ''

    # Summary by flow_type
    summary = {}
    for ft in ['clockin_alarm', 'clockout_alarm', 'late_clockin',
               'overtime', 'deviation', 'request_reminder', 'morning_check']:
        summary[ft] = 0
    for a in alerts_today.data:
        ft = a['flow_type']
        summary[ft] = summary.get(ft, 0) + 1

    # Store cards / NeeSaカードは互いに独立した外部API呼び出しのため、
    # 直列実行だと遅いのでスレッドで並行実行する(体感速度改善)
    def _fetch_store_cards():
        try:
            return _get_store_shifts_and_attendance(today)
        except Exception:
            emp_data = supabase.table('employees') \
                .select('*, mappings(lw_account_id)') \
                .order('employee_code') \
                .execute().data
            return [], emp_data

    def _fetch_neesa_groups():
        # シフト予定(登録シフト)自体は日付を問わず表示する。KoTの実打刻オーバーレイは
        # 未来日には存在しない(まだ打刻されていない)ため、当日・過去日のみ適用する。
        try:
            import neesa_lw as _neesa_lw
            import neesa_kot as _neesa_kot
            now_jst = datetime.now(JST)
            groups = _neesa_lw.get_today_shifts(selected)
            for g in groups:
                for s in g['shifts']:
                    s.setdefault('status', 'scheduled')
            if not is_future:
                groups = _neesa_kot.apply_today(groups, now_jst)
            return groups
        except Exception as e:
            app.logger.warning('dashboard NeeSaカード取得失敗: %s', e)
            return []

    def _fetch_unified_status():
        if not is_today:
            return []
        try:
            import attendance_unified as _attendance_unified
            return _attendance_unified.get_unified_status()
        except Exception as e:
            app.logger.warning('リアルタイム出勤状況取得失敗: %s', e)
            return []

    with ThreadPoolExecutor(max_workers=3) as _ex:
        _store_future = _ex.submit(_fetch_store_cards)
        _neesa_future = _ex.submit(_fetch_neesa_groups)
        _unified_future = _ex.submit(_fetch_unified_status)
        store_cards, all_emp_data = _store_future.result()
        neesa_groups = _neesa_future.result()
        raw_unified_status = _unified_future.result()

    # ===== リアルタイム出勤状況(本体/NeeSa両テナント統合。/my/overviewと同じロジックを無認証で公開) =====
    unified_grouped = {}
    unified_group_list = []
    if is_today:
        try:
            import neesa_lw
            # 本日出勤予定のある人だけに絞る(店舗シフト予定+NeeSaシフト予定の名前集合)
            scheduled_names_today = set()
            for card in store_cards:
                for stf in card.get('staff_scheduled', []):
                    scheduled_names_today.add(stf['name'])
            for g in neesa_groups:
                for stf in g.get('shifts', []):
                    scheduled_names_today.add(stf['name'])

            for st in raw_unified_status:
                if st['display_name'] not in scheduled_names_today:
                    continue
                key = f"{st['company']}|{st['dept']}"
                unified_grouped.setdefault(key, []).append(st)
            unified_group_list = sorted(
                [(key, key.replace('|', ' / ')) for key in unified_grouped.keys()],
                key=lambda kv: (
                    neesa_lw.COMPANY_ORDER.index(kv[0].split('|')[0]) if kv[0].split('|')[0] in neesa_lw.COMPANY_ORDER else 99,
                    neesa_lw.DEPT_ORDER.index(kv[0].split('|')[1]) if kv[0].split('|')[1] in neesa_lw.DEPT_ORDER else 99,
                )
            )
        except Exception as e:
            app.logger.warning('リアルタイム出勤状況取得失敗: %s', e)
            unified_grouped = {}
            unified_group_list = []

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

    # 過去日表示時はアラート履歴を生成（時刻昇順）
    alert_history = []
    if is_past:
        for a in alerts_today.data:
            ek = a.get('employee_key', '')
            if ek == '__admin__':
                continue
            emp = a.get('employees') or {}
            name = (emp.get('last_name', '') + emp.get('first_name', '')).strip() or '?'
            store = emp.get('division_name', '') or ''
            ft = a['flow_type']
            time_str = ''
            raw_ts = a.get('sent_at') or a.get('created_at') or ''
            if raw_ts:
                try:
                    dt = _parse_iso(raw_ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    time_str = dt.astimezone(JST).strftime('%H:%M')
                except (ValueError, TypeError):
                    pass
            alert_history.append({
                'time': time_str,
                'name': name,
                'store': store,
                'label': FLOW_LABELS.get(ft, ft),
                'flow_type': ft,
            })
        alert_history.sort(key=lambda x: x['time'])

    return render_template('dashboard.html',
                           today=today,
                           today_actual=today_actual,
                           max_future_date=max_future_date,
                           marquee_text=marquee_text,
                           active_tab=active_tab,
                           unified_grouped=unified_grouped,
                           unified_group_list=unified_group_list,
                           is_past=is_past,
                           is_today=is_today,
                           is_future=is_future,
                           prev_date=prev_date,
                           next_date=next_date,
                           alert_history=alert_history,
                           summary=summary,
                           total_today=len(alerts_today.data),
                           unmapped_count=unmapped_count,
                           unmapped_names=unmapped[:5],
                           neesa_groups=neesa_groups,
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

    # 異体字警告検出
    variant_warnings = []
    for e in employees.data:
        ln = e.get('last_name', '') or ''
        fn = e.get('first_name', '') or ''
        norm_ln = _normalize_name(ln)
        norm_fn = _normalize_name(fn)
        if norm_ln != ln or norm_fn != fn:
            original = ln + fn
            normalized = norm_ln + norm_fn
            variant_warnings.append({
                'code': e.get('employee_code', ''),
                'name': ln + ' ' + fn,
                'normalized': norm_ln + ' ' + norm_fn,
            })

    return render_template('staff.html',
                           employees=employees.data,
                           variant_warnings=variant_warnings)


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
    import kot_api
    flow_type = request.args.get('flow_type', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    unapplied_list = []
    is_unapplied = (flow_type == 'unapplied')

    if is_unapplied:
        # 未申請者抽出: deviation通知ありでKoT残業申請がないスタッフ
        target_date = date_from or date.today().isoformat()
        dev_result = supabase.table('alerts_sent') \
            .select('employee_key, employees(employee_code, last_name, first_name)') \
            .eq('alert_date', target_date) \
            .eq('flow_type', 'deviation') \
            .execute()
        # KoTの残業申請を確認
        td = datetime.strptime(target_date, '%Y-%m-%d')
        applied_keys = set()
        if not kot_api.is_api_blocked():
            try:
                ot_data = kot_api.get_overtime_requests(td.year, td.month)
                if ot_data and 'overtimeRequests' in ot_data:
                    for req in ot_data['overtimeRequests']:
                        applied_keys.add(req.get('employeeKey', ''))
            except Exception:
                pass
        seen_keys = set()
        for d in dev_result.data:
            ek = d['employee_key']
            if ek in seen_keys:
                continue
            seen_keys.add(ek)
            if ek not in applied_keys:
                emp = d.get('employees') or {}
                unapplied_list.append({
                    'employee_key': ek,
                    'code': emp.get('employee_code', ''),
                    'name': (emp.get('last_name', '') + ' ' + emp.get('first_name', '')).strip(),
                    'date': target_date,
                })

    query = supabase.table('alerts_sent') \
        .select('*, employees(last_name, first_name)') \
        .order('created_at', desc=True)

    if flow_type and not is_unapplied:
        query = query.eq('flow_type', flow_type)
    if is_unapplied:
        target_date = date_from or date.today().isoformat()
        query = query.eq('alert_date', target_date).in_('flow_type', ['deviation', 'request_reminder'])
    else:
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
                           is_unapplied=is_unapplied)


@app.route('/logs/reset', methods=['POST'])
def logs_reset():
    reset_date = request.form.get('reset_date', '')
    employee_key = request.form.get('employee_key', '').strip()
    flow_type = request.form.get('flow_type', '').strip()

    if not reset_date:
        flash('日付を指定してください', 'error')
        return redirect(url_for('logs'))

    query = supabase.table('alerts_sent').delete().eq('alert_date', reset_date)
    if employee_key:
        query = query.eq('employee_key', employee_key)
    if flow_type:
        query = query.eq('flow_type', flow_type)
    result = query.execute()

    count = len(result.data) if result.data else 0
    flash(f'{reset_date} の通知履歴を {count} 件削除しました', 'success')
    return redirect(url_for('logs'))

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

    range_param = request.args.get('range', 'week')
    if range_param not in ('day', 'week', 'month'):
        range_param = 'week'
    today = date.today()
    if range_param == 'day':
        dates = [today]
    elif range_param == 'month':
        import calendar as _cal_mod
        days_in_month = _cal_mod.monthrange(today.year, today.month)[1]
        last_day_of_month = date(today.year, today.month, days_in_month)
        span = (last_day_of_month - today).days + 1
        dates = [(today + timedelta(days=i)) for i in range(span)]
    else:
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

    # ===== NeeSa/アソビバ本社/ディアメント 各部署タブ(表示するかはユーザーが選択) =====
    import neesa_lw
    all_neesa_keys = sorted(
        set(neesa_lw.DEPT_MAP.values()) | set(neesa_lw.ALWAYS_SHOW),
        key=lambda kv: (
            neesa_lw.COMPANY_ORDER.index(kv[0]) if kv[0] in neesa_lw.COMPANY_ORDER else 99,
            neesa_lw.DEPT_ORDER.index(kv[1]) if kv[1] in neesa_lw.DEPT_ORDER else 99,
        )
    )
    is_neesa_active = active_store in [f'{c}|{d}' for c, d in all_neesa_keys]

    neesa_by_key = {key: {d.isoformat(): [] for d in dates} for key in all_neesa_keys}
    if is_neesa_active:
        # アクティブなタブがNeeSa側の時のみ実際にカレンダーを取得。
        # 日数分の往復を避けるため、範囲をまとめて1回で取得する
        shifts_by_date = neesa_lw.get_shifts_grouped_by_date_range(dates[0], dates[-1])
        for d in dates:
            d_str = d.isoformat()
            for g in shifts_by_date.get(d_str, []):
                key = (g['company'], g['dept'])
                if key in neesa_by_key:
                    neesa_by_key[key][d_str] = sorted(g['shifts'], key=lambda x: x.get('start') or '')

    neesa_shifts = []
    for company, dept in all_neesa_keys:
        days = []
        for d in dates:
            d_str = d.isoformat()
            days.append({
                'date': d_str,
                'weekday': ['月', '火', '水', '木', '金', '土', '日'][d.weekday()],
                'is_today': d == today,
                'shifts': neesa_by_key[(company, dept)][d_str],
            })
        neesa_shifts.append({
            'company': company,
            'dept': dept,
            'tab_key': f'{company}|{dept}',
            'days': days,
        })

    # タブバー表示用に会社ごとへグルーピング(順序はCOMPANY_ORDER/DEPT_ORDER準拠)
    neesa_companies = []
    for entry in neesa_shifts:
        if not neesa_companies or neesa_companies[-1]['company'] != entry['company']:
            neesa_companies.append({'company': entry['company'], 'groups': []})
        neesa_companies[-1]['groups'].append(entry)

    # ログイン中の本人がNeeSaテナントの場合のみ、自分の予定を編集・削除できるようにする
    # (トレコレ店舗側は別のカレンダー基盤で書き込みAPIが未整備のため対象外)
    import json
    my_name = session.get('staff_name') or ''
    my_events_json = '{}'
    if my_name and is_neesa_active:
        try:
            import lw_calendar_write
            my_events = lw_calendar_write.get_my_events(my_name, dates[0], dates[-1])
            my_events_json = json.dumps(my_events, ensure_ascii=False)
        except Exception as e:
            app.logger.warning('shifts自分の予定取得失敗: %s', e)

    return render_template('shifts.html',
                           store_shifts=store_shifts,
                           neesa_shifts=neesa_shifts,
                           neesa_companies=neesa_companies,
                           active_store=active_store,
                           today=today_str,
                           range_param=range_param,
                           my_name=my_name,
                           my_events_json=my_events_json)


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
        # アラート文言テンプレート保存（JSONファイル）
        import db_supabase as _db
        templates = _db.get_alert_templates()
        template_types = ['clockin_alarm', 'late_clockin', 'clockout_alarm',
                          'overtime', 'deviation', 'request_reminder', 'morning_check']
        for ft in template_types:
            tmpl_val = request.form.get('template_' + ft, '').strip()
            if tmpl_val:
                templates[ft] = tmpl_val
        _db.save_alert_templates(templates)

        updates = {
            'dashboard_marquee_text': request.form.get('dashboard_marquee_text', '').strip(),
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
        try:
            supabase.table('alert_settings').update(updates).eq('id', 1).execute()
            flash('設定を保存しました', 'success')
        except Exception:
            # dashboard_marquee_text列が未作成の場合はそれを除いて再試行
            updates.pop('dashboard_marquee_text', None)
            supabase.table('alert_settings').update(updates).eq('id', 1).execute()
            flash('設定を保存しました（インフォメーション設定はDB未対応のため保存されていません）', 'error')
        return redirect(url_for('settings'))

    try:
        result = supabase.table('alert_settings').select('*').eq('id', 1).execute()
        s = result.data[0] if result.data else {}
    except Exception:
        s = {}
        flash('alert_settingsテーブルが見つかりません。Supabase SQL Editorでテーブルを作成してください。', 'error')

    # アラート文言テンプレート取得（JSONファイル）
    import db_supabase as _db
    templates = _db.get_alert_templates()
    return render_template('settings.html', s=s, templates=templates)


@app.route('/callback', methods=['POST'])
def callback():
    data = request.json or {}
    print(f"[CALLBACK] received: {data}", flush=True)
    channel_id = data.get('source', {}).get('channelId', '')
    if channel_id:
        print(f"[CALLBACK] channelId: {channel_id}", flush=True)
    return 'OK', 200


@app.route('/board')
def board():
    """勤怠ボード（モニタ常時表示用キオスク画面・NeeSaカレンダー由来）
    ?date=YYYY-MM-DD で前後±35日のシフトを閲覧可。当日のみKoTライブ色付け対象。"""
    import neesa_lw
    from collections import OrderedDict
    now = datetime.now(JST)
    today = now.date()
    qd = (request.args.get('date') or '').strip()
    try:
        target = date.fromisoformat(qd) if qd else today
    except ValueError:
        target = today
    # ±35日にクランプ
    delta = (target - today).days
    if delta > 35:
        target = today + timedelta(days=35)
    elif delta < -35:
        target = today - timedelta(days=35)
    is_today = (target == today)

    groups = neesa_lw.get_today_shifts(target)
    for g in groups:
        for s in g['shifts']:
            s.setdefault('status', 'scheduled')
    # 当日のみ NeeSa KoT の打刻で色付け＋打刻のみ者を追加（過去/未来日は予定表示のまま）
    if is_today:
        try:
            import neesa_kot
            neesa_kot.apply_today(groups, now)
        except Exception as e:
            app.logger.warning('board KoT色付け失敗: %s', e)
    companies = OrderedDict()
    total = 0
    for g in groups:
        companies.setdefault(g['company'], []).append(g)
        total += len(g['shifts'])
    # 未マッピング者（既定の発送に表示中）を通知用に収集
    unmapped = sorted({s['name'] for g in groups for s in g['shifts']
                       if s.get('unmapped')})

    weekdays = ['月', '火', '水', '木', '金', '土', '日']
    title = target.strftime('%Y/%m/%d') + '（' + weekdays[target.weekday()] + '）'
    return render_template(
        'board.html', companies=companies, total=total, unmapped=unmapped,
        updated=now.strftime('%H:%M'), today=title, is_today=is_today,
        target=target.isoformat(),
        prev_day=(target - timedelta(days=1)).isoformat(),
        next_day=(target + timedelta(days=1)).isoformat(),
        prev_week=(target - timedelta(days=7)).isoformat(),
        next_week=(target + timedelta(days=7)).isoformat(),
    )


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
