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
        return redirect(url_for('my.home'))

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


@my_bp.route('/')
@require_staff_login
def home():
    staff = _get_staff(session['staff_id'])
    clock_in, clock_out = _today_records(staff)
    return render_template('my_home.html', staff=staff, clock_in=clock_in, clock_out=clock_out)


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
        if not (title and date_str and start_time and end_time):
            flash('全項目を入力してください', 'error')
            return redirect(url_for('my.schedule'))
        try:
            start_dt = datetime.strptime(f'{date_str} {start_time}', '%Y-%m-%d %H:%M')
            end_dt = datetime.strptime(f'{date_str} {end_time}', '%Y-%m-%d %H:%M')
        except ValueError:
            flash('日付・時刻の形式が正しくありません', 'error')
            return redirect(url_for('my.schedule'))
        summary = f'{start_time}-{end_time}{staff["display_name"]} {title}'.strip()
        ok, detail = lw_calendar_write.create_event(summary, start_dt, end_dt)
        if ok:
            flash('予定を登録しました', 'success')
        else:
            flash('予定の登録に失敗しました', 'error')
        return redirect(url_for('my.schedule'))

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

    selected_date = request.args.get('date') or today_str
    selected_names = names_by_date.get(selected_date, [])

    prev_month = (first_day - timedelta(days=1)).strftime('%Y-%m')
    next_month = (last_day + timedelta(days=1)).strftime('%Y-%m')

    return render_template(
        'my_schedule.html', staff=staff, today=today_str,
        weekday_ja=weekday_ja_sun, weeks=weeks, names_by_date=names_by_date,
        month_label=f'{year}年{month}月', current_month=f'{year:04d}-{month:02d}',
        prev_month=prev_month, next_month=next_month,
        selected_date=selected_date, selected_names=selected_names,
    )


@my_bp.route('/color', methods=['POST'])
@require_staff_login
def set_color():
    color = (request.form.get('bg_color') or '').strip()
    if color:
        supabase.table('staff_directory').update({'bg_color': color}) \
            .eq('staff_id', session['staff_id']).execute()
    return redirect(url_for('my.home'))
