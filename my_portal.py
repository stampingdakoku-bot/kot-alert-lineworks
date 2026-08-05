"""
個人向けポータル(/my/*): 勤怠ボタン・社内全体の出勤状況一覧・スケジュール登録。

【暫定措置】本来はLINE WORKS SSO(OIDC)でログインする設計だが、開発者コンソール側の
原因不明のエラーによりログイン実地テストが通らないため、氏名を一覧から選ぶだけの
簡易ログインで暫定運用する(2026-08-04)。session['staff_id']は既存の
session['authenticated'](パスコード認証、現在無効化)とは別名・別用途で、互いに影響しない。
SSOが解決次第、login()の中身だけをAuthlib実装に差し替える想定。
既存の管理画面(/, /board, /staff等)はこれまで通り無認証のまま・無変更。
"""
import functools
import json
import calendar as _calendar_mod
from datetime import datetime, date, timezone, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from supabase import create_client
import os
from dotenv import load_dotenv

import kot_write
import attendance_unified
import lw_calendar_write
import neesa_lw

load_dotenv()
JST = timezone(timedelta(hours=9))

supabase = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_ROLE_KEY'))

my_bp = Blueprint('my', __name__, url_prefix='/my')

FLOW_LABELS = {
    'clockin_alarm': '出勤アラーム',
    'clockout_alarm': '退勤アラーム',
    'late_clockin': '出勤打刻なし',
    'overtime': '超過警告',
    'deviation': '乖離通知',
    'request_reminder': '申請リマインド',
    'morning_check': '翌朝チェック',
}


NOTICE_SENDER_EXTRA = {'矢垰'}  # 総務部以外で個人お知らせの送信権限を持つ人


def _can_send_notice(staff):
    return staff.get('dept') == '総務部' or staff.get('display_name') in NOTICE_SENDER_EXTRA


def _my_notifications(staff, limit=20):
    """本人宛の自動アラート通知(alerts_sent)と総務等からの個人お知らせ(personal_notices)を
    まとめて新しい順で返す。alerts_sentはトレコレ本体テナントのみ運用のためNeeSaテナントの
    スタッフでは常に空になる。personal_noticesテーブル未作成の環境でも壊れないようtry/exceptで守る。"""
    items = []
    key = staff.get('kot_employee_key')
    if key:
        try:
            result = supabase.table('alerts_sent').select('*') \
                .eq('employee_key', key).order('created_at', desc=True).limit(limit).execute()
            for a in result.data:
                items.append({
                    'created_at': a.get('created_at'),
                    'kind': 'alert',
                    'flow_type': a.get('flow_type'),
                    'message': a.get('message') or '',
                    'sender_name': None,
                })
        except Exception:
            pass
    try:
        result2 = supabase.table('personal_notices') \
            .select('*, sender:sender_staff_id(display_name)') \
            .eq('recipient_staff_id', staff['staff_id']) \
            .order('created_at', desc=True).limit(limit).execute()
        for n in result2.data:
            sender = (n.get('sender') or {}).get('display_name') or '総務'
            items.append({
                'created_at': n.get('created_at'),
                'kind': 'notice',
                'flow_type': None,
                'message': n.get('message') or '',
                'sender_name': sender,
            })
    except Exception:
        pass
    items.sort(key=lambda x: x['created_at'] or '', reverse=True)
    return items[:limit]


def _unread_notice_count(staff_id):
    try:
        r = supabase.table('personal_notices').select('id', count='exact') \
            .eq('recipient_staff_id', staff_id).eq('is_read', False).execute()
        return r.count or 0
    except Exception:
        return 0


def _mark_notices_read(staff_id):
    try:
        supabase.table('personal_notices').update({'is_read': True}) \
            .eq('recipient_staff_id', staff_id).eq('is_read', False).execute()
    except Exception:
        pass


def require_staff_login(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('staff_id'):
            return redirect(url_for('my.login'))
        return view(*args, **kwargs)
    return wrapped


def _get_staff_list():
    result = supabase.table('staff_directory').select('*').eq('is_active', True) \
        .order('company').order('dept').order('display_name').execute()
    return result.data


def _get_staff(staff_id):
    result = supabase.table('staff_directory').select('*').eq('staff_id', staff_id).limit(1).execute()
    return result.data[0] if result.data else None


def _build_attendees(staff, extra_json):
    """ログイン中の本人を必ず参加メンバーに含め、検索UIで追加選択された人をマージする"""
    attendees = []
    seen = set()
    if staff.get('lw_account_id'):
        attendees.append({'email': staff['lw_account_id'], 'displayName': staff['display_name']})
        seen.add(staff['lw_account_id'])
    try:
        extra = json.loads(extra_json or '[]')
    except (ValueError, TypeError):
        extra = []
    for a in extra:
        email = (a or {}).get('email')
        if email and email not in seen:
            attendees.append({'email': email, 'displayName': a.get('displayName', '')})
            seen.add(email)
    return attendees or None


def _build_reminders(triggers_json):
    try:
        triggers = json.loads(triggers_json or '[]')
    except (ValueError, TypeError):
        triggers = []
    reminders = [{'method': 'DISPLAY', 'trigger': t} for t in triggers if t]
    return reminders or None


def _login_landing_url(staff):
    """ログイン直後の遷移先を決める。
    未打刻: マイメニュー(打刻用)。出勤済み・退勤済み: 本日の出勤状況。
    出勤済みだが退勤前で、退勤予定時刻の1時間前を過ぎている場合は
    打刻漏れ防止のためマイメニューへ戻す。"""
    clock_in, clock_out = _today_records(staff)
    if not clock_in:
        return url_for('my.home')
    if clock_out:
        return url_for('dashboard')
    if staff.get('kot_tenant') == 'neesa':
        try:
            today = datetime.now(JST).date()
            shift_end = None
            for g in neesa_lw.get_today_shifts(today):
                for s in g.get('shifts', []):
                    if s.get('name') == staff['display_name'] and s.get('end'):
                        shift_end = s['end']
                        break
                if shift_end:
                    break
            if shift_end:
                h, m = (int(x) for x in shift_end.split(':'))
                now = datetime.now(JST)
                end_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if now >= end_dt - timedelta(hours=1):
                    return url_for('my.home')
        except Exception:
            pass
    return url_for('dashboard')


@my_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        staff_id = request.form.get('staff_id')
        staff = _get_staff(staff_id) if staff_id else None
        if not staff:
            flash('選択が正しくありません', 'error')
            return redirect(url_for('my.login'))
        session['staff_id'] = staff['staff_id']
        session['staff_name'] = staff['display_name']
        session['bg_color'] = staff.get('bg_color')
        return redirect(_login_landing_url(staff))

    grouped = {}
    for s in _get_staff_list():
        grouped.setdefault((s['company'], s['dept']), []).append(s)
    return render_template('my_login.html', grouped=grouped)


@my_bp.route('/logout')
def logout():
    session.pop('staff_id', None)
    session.pop('staff_name', None)
    return redirect(url_for('my.login'))


def _today_records(staff):
    if not staff.get('kot_employee_key'):
        return None, None
    today = datetime.now(JST).strftime('%Y-%m-%d')
    records = kot_write.get_today_records(staff['kot_tenant'], staff['kot_employee_key'], today)
    clock_in = next((r['time'] for r in records if r.get('code') == '1'), None)
    clock_out = next((r['time'] for r in records if r.get('code') == '2'), None)
    return clock_in, clock_out


def _next_shift_date(staff):
    """次回の出勤予定日(月/日(曜)表記)を返す。NeeSaテナントはシフトカレンダーから
    算出、本体テナントは同等の簡易照会が未実装のためNoneを返す。"""
    if staff.get('kot_tenant') != 'neesa':
        return None
    tomorrow = (datetime.now(JST) + timedelta(days=1)).date()
    names_by_date = neesa_lw.get_names_by_date_range(tomorrow, tomorrow + timedelta(days=30))
    weekday_ja = ['月', '火', '水', '木', '金', '土', '日']
    for d in sorted(names_by_date):
        if staff['display_name'] in names_by_date[d]:
            d_obj = datetime.strptime(d, '%Y-%m-%d')
            return f'{d_obj.month}月{d_obj.day}日（{weekday_ja[d_obj.weekday()]}）'
    return None


@my_bp.route('/')
@require_staff_login
def home():
    staff = _get_staff(session['staff_id'])
    clock_in, clock_out = _today_records(staff)
    next_shift_date = _next_shift_date(staff) if clock_out else None
    unread_notice_count = _unread_notice_count(staff['staff_id'])
    notifications = _my_notifications(staff)
    _mark_notices_read(staff['staff_id'])
    return render_template('my_home.html', staff=staff, clock_in=clock_in, clock_out=clock_out,
                           next_shift_date=next_shift_date, notifications=notifications,
                           unread_notice_count=unread_notice_count,
                           can_send_notice=_can_send_notice(staff),
                           flow_labels=FLOW_LABELS)


@my_bp.route('/punch', methods=['POST'])
@require_staff_login
def punch():
    staff = _get_staff(session['staff_id'])
    if not staff.get('kot_employee_key'):
        flash('あなたはKoTアカウントと未紐付けのため、打刻できません', 'error')
        return redirect(url_for('my.home'))

    code = request.form.get('code')
    if code not in ('1', '2'):
        flash('不正な操作です', 'error')
        return redirect(url_for('my.home'))
    now = datetime.now(JST)
    ok, status_code, body = kot_write.submit_timerecord(
        staff['kot_tenant'], staff['kot_employee_key'], code, now)
    supabase.table('punch_audit_log').insert({
        'staff_id': staff['staff_id'],
        'kot_tenant': staff['kot_tenant'],
        'employee_key': staff['kot_employee_key'],
        'code': code,
        'submitted_time': now.isoformat(),
        'response_status': status_code,
        'response_body': (body or '')[:2000],
    }).execute()
    if ok:
        flash(('出勤' if code == '1' else '退勤') + 'を記録しました', 'success')
    else:
        flash('打刻に失敗しました。時間をおいて再度お試しください', 'error')
    return redirect(url_for('my.home'))


@my_bp.route('/overview', methods=['GET', 'POST'])
@require_staff_login
def overview():
    staff = _get_staff(session['staff_id'])
    prefs = attendance_unified.get_preferences(staff['staff_id'])

    all_status = attendance_unified.get_unified_status()
    grouped = {}
    for s in all_status:
        grouped.setdefault(f"{s['company']}|{s['dept']}", []).append(s)
    group_list = [(key, key.replace('|', ' / ')) for key in grouped.keys()]

    if request.method == 'POST':
        shown = set(request.form.getlist('show'))
        hidden = [key for key, _ in group_list if key not in shown]
        attendance_unified.save_preferences(staff['staff_id'], hidden)
        return redirect(url_for('my.overview'))

    hidden_set = set(prefs.get('hidden_names') or [])
    return render_template('my_overview.html', staff=staff, grouped=grouped, group_list=group_list, hidden_set=hidden_set)


@my_bp.route('/schedule', methods=['GET', 'POST'])
@require_staff_login
def schedule():
    staff = _get_staff(session['staff_id'])
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        date_str = request.form.get('date')
        start_time = request.form.get('start_time')
        end_time = request.form.get('end_time')
        repeat_freq = request.form.get('repeat_freq') or ''
        repeat_days = request.form.getlist('repeat_days')
        month_ctx = date_str[:7] if date_str else None
        if not (title and date_str and start_time and end_time):
            flash('全項目を入力してください', 'error')
            return redirect(url_for('my.schedule', month=month_ctx))
        try:
            start_dt = datetime.strptime(f'{date_str} {start_time}', '%Y-%m-%d %H:%M')
            end_dt = datetime.strptime(f'{date_str} {end_time}', '%Y-%m-%d %H:%M')
        except ValueError:
            flash('日付・時刻の形式が正しくありません', 'error')
            return redirect(url_for('my.schedule', month=month_ctx))
        summary = f'{start_time}-{end_time}{staff["display_name"]} {title}'.strip()
        recurrence = None
        if repeat_freq in ('weekly', 'monthly') and repeat_days:
            recurrence = [lw_calendar_write.build_rrule(repeat_freq, repeat_days)]
        attendees = _build_attendees(staff, request.form.get('attendees'))
        reminders = _build_reminders(request.form.get('reminders'))
        ok, detail = lw_calendar_write.create_event(
            summary, start_dt, end_dt, recurrence=recurrence,
            attendees=attendees, reminders=reminders)
        if ok:
            flash('予定を登録しました', 'success')
        else:
            flash('予定の登録に失敗しました', 'error')
        return redirect(url_for('my.schedule', month=month_ctx))

    now = datetime.now(JST)
    today_str = now.strftime('%Y-%m-%d')
    month_str = request.args.get('month')
    if month_str:
        try:
            year, month = (int(x) for x in month_str.split('-'))
        except ValueError:
            year, month = now.year, now.month
    else:
        year, month = now.year, now.month

    first_day = date(year, month, 1)
    last_day = date(year, month, _calendar_mod.monthrange(year, month)[1])
    names_by_date = neesa_lw.get_names_by_date_range(first_day, last_day)
    shift_labels_by_date = neesa_lw.get_shift_labels_by_date_range(first_day, last_day)
    my_events_by_date = lw_calendar_write.get_my_events_all(staff['display_name'], first_day, last_day)

    weekday_ja_sun = ['日', '月', '火', '水', '木', '金', '土']
    start_offset = (first_day.weekday() + 1) % 7  # 月曜=0 → 日曜=0起点に変換
    weeks = []
    week = [None] * start_offset
    d = first_day
    while d <= last_day:
        week.append(d)
        if len(week) == 7:
            weeks.append(week)
            week = []
        d += timedelta(days=1)
    if week:
        weeks.append(week + [None] * (7 - len(week)))

    prev_month = (first_day - timedelta(days=1)).strftime('%Y-%m')
    next_month = (last_day + timedelta(days=1)).strftime('%Y-%m')

    staff_options = [
        {'email': s['lw_account_id'], 'displayName': s['display_name'], 'dept': f"{s['company']} / {s['dept']}"}
        for s in _get_staff_list() if s.get('lw_account_id') and s['staff_id'] != staff['staff_id']
    ]

    return render_template(
        'my_schedule.html', staff=staff, today=today_str,
        weekday_ja=weekday_ja_sun, weeks=weeks,
        names_by_date=names_by_date, shift_labels_by_date=shift_labels_by_date,
        my_events_by_date=my_events_by_date,
        month_label=f'{year}年{month}月', current_month=f'{year:04d}-{month:02d}',
        prev_month=prev_month, next_month=next_month,
        staff_options_json=json.dumps(staff_options, ensure_ascii=False),
        calendar_data_json=json.dumps({
            'names': names_by_date,
            'myEvents': my_events_by_date,
        }, ensure_ascii=False),
    )


@my_bp.route('/schedule/event/<event_id>/edit', methods=['POST'])
@require_staff_login
def schedule_edit(event_id):
    staff = _get_staff(session['staff_id'])
    title = (request.form.get('title') or '').strip()
    date_str = request.form.get('date')
    start_time = request.form.get('start_time')
    end_time = request.form.get('end_time')
    recurrence_json = request.form.get('recurrence') or '[]'
    calendar_id = request.form.get('calendar_id') or None
    month_ctx = date_str[:7] if date_str else None
    next_url = request.form.get('next') or ''
    fallback = next_url if next_url.startswith('/') else url_for('my.schedule', month=month_ctx)

    if not (title and date_str and start_time and end_time):
        flash('全項目を入力してください', 'error')
        return redirect(fallback)
    try:
        start_dt = datetime.strptime(f'{date_str} {start_time}', '%Y-%m-%d %H:%M')
        end_dt = datetime.strptime(f'{date_str} {end_time}', '%Y-%m-%d %H:%M')
    except ValueError:
        flash('日付・時刻の形式が正しくありません', 'error')
        return redirect(fallback)
    try:
        recurrence = json.loads(recurrence_json)
    except (ValueError, TypeError):
        recurrence = []
    if recurrence:
        # 開始時刻を変更した場合、既存のEXDATE(除外occurrence)の時刻もずれるため
        # 新しい開始時刻に合わせて付け替える(でないと削除済みのoccurrenceが復活する)
        recurrence = lw_calendar_write.remap_exdate_times(recurrence, start_dt.time())

    summary = f'{start_time}-{end_time}{staff["display_name"]} {title}'.strip()
    attendees = _build_attendees(staff, request.form.get('attendees'))
    reminders = _build_reminders(request.form.get('reminders'))
    ok, detail = lw_calendar_write.update_event(
        event_id, summary, start_dt, end_dt, recurrence=recurrence or None,
        attendees=attendees, reminders=reminders, calendar_id=calendar_id)
    if ok:
        flash('予定を更新しました', 'success')
    else:
        flash('予定の更新に失敗しました', 'error')
    return redirect(fallback)


@my_bp.route('/schedule/event/<event_id>/delete', methods=['POST'])
@require_staff_login
def schedule_delete(event_id):
    mode = request.form.get('mode', 'all')  # 'all' | 'single' | 'following'
    occurrence_date_str = request.form.get('occurrence_date') or ''
    series_start_str = request.form.get('series_start') or ''
    series_end_str = request.form.get('series_end') or ''
    summary = request.form.get('summary') or ''
    recurrence_json = request.form.get('recurrence') or '[]'
    calendar_id = request.form.get('calendar_id') or None
    month_ctx = (occurrence_date_str or series_start_str)[:7] or None
    next_url = request.form.get('next') or ''
    fallback = next_url if next_url.startswith('/') else url_for('my.schedule', month=month_ctx)

    try:
        recurrence = json.loads(recurrence_json)
    except (ValueError, TypeError):
        recurrence = []

    if mode == 'all' or not recurrence:
        ok, detail = lw_calendar_write.delete_event(event_id, calendar_id=calendar_id)
    else:
        try:
            series_start = datetime.fromisoformat(series_start_str)
            series_end = datetime.fromisoformat(series_end_str)
            occ_date = date.fromisoformat(occurrence_date_str)
        except (ValueError, TypeError):
            flash('削除処理に失敗しました', 'error')
            return redirect(fallback)

        if mode == 'single':
            occ_dt = datetime.combine(occ_date, series_start.time())
            new_recurrence = lw_calendar_write.exclude_occurrence(recurrence, occ_dt)
        else:  # following
            new_recurrence = lw_calendar_write.truncate_recurrence_before(recurrence, occ_date)
        ok, detail = lw_calendar_write.update_event(
            event_id, summary, series_start, series_end, recurrence=new_recurrence, calendar_id=calendar_id)

    if ok:
        flash('予定を削除しました', 'success')
    else:
        flash('予定の削除に失敗しました', 'error')
    return redirect(fallback)


@my_bp.route('/notify', methods=['GET', 'POST'])
@require_staff_login
def notify():
    staff = _get_staff(session['staff_id'])
    if not _can_send_notice(staff):
        flash('この機能を利用する権限がありません', 'error')
        return redirect(url_for('my.home'))

    if request.method == 'POST':
        action = request.form.get('action', 'send')
        try:
            if action == 'add_template':
                text = (request.form.get('template_text') or '').strip()
                if text:
                    supabase.table('notice_templates').insert({'text': text}).execute()
                    flash('定型文を追加しました', 'success')
                return redirect(url_for('my.notify'))
            if action == 'delete_template':
                template_id = request.form.get('template_id')
                if template_id:
                    supabase.table('notice_templates').delete().eq('id', template_id).execute()
                    flash('定型文を削除しました', 'success')
                return redirect(url_for('my.notify'))

            recipient_id = request.form.get('recipient_id')
            message = (request.form.get('message') or '').strip()
            if not (recipient_id and message):
                flash('宛先とメッセージを入力してください', 'error')
                return redirect(url_for('my.notify'))
            supabase.table('personal_notices').insert({
                'sender_staff_id': staff['staff_id'],
                'recipient_staff_id': recipient_id,
                'message': message,
            }).execute()
            flash('お知らせを送信しました', 'success')
        except Exception:
            flash('処理に失敗しました（DBのテーブルが未作成の可能性があります）', 'error')
        return redirect(url_for('my.notify'))

    recipients = [s for s in _get_staff_list() if s['staff_id'] != staff['staff_id']]
    try:
        templates = supabase.table('notice_templates').select('*').order('created_at').execute().data
    except Exception:
        templates = []
    return render_template('my_notify.html', staff=staff, recipients=recipients, templates=templates)


@my_bp.route('/color', methods=['POST'])
@require_staff_login
def set_color():
    color = (request.form.get('bg_color') or '').strip()
    if color:
        try:
            supabase.table('staff_directory').update({'bg_color': color}) \
                .eq('staff_id', session['staff_id']).execute()
            session['bg_color'] = color
        except Exception:
            flash('背景色の保存に失敗しました（準備中です）', 'error')
    return redirect(url_for('my.home'))
