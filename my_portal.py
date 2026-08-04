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
from datetime import datetime, timezone, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from supabase import create_client
import os
from dotenv import load_dotenv

import kot_write
import attendance_unified
import lw_calendar_write

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


@my_bp.route('/')
@require_staff_login
def home():
    staff = _get_staff(session['staff_id'])
    return render_template('my_home.html', staff=staff)


@my_bp.route('/punch', methods=['GET', 'POST'])
@require_staff_login
def punch():
    staff = _get_staff(session['staff_id'])
    if not staff.get('kot_employee_key'):
        flash('あなたはKoTアカウントと未紐付けのため、打刻できません', 'error')
        return redirect(url_for('my.home'))

    if request.method == 'POST':
        code = request.form.get('code')
        if code not in ('1', '2'):
            flash('不正な操作です', 'error')
            return redirect(url_for('my.punch'))
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
        return redirect(url_for('my.punch'))

    today = datetime.now(JST).strftime('%Y-%m-%d')
    records = kot_write.get_today_records(staff['kot_tenant'], staff['kot_employee_key'], today)
    clock_in = next((r['time'] for r in records if r.get('code') == '1'), None)
    clock_out = next((r['time'] for r in records if r.get('code') == '2'), None)
    return render_template('my_punch.html', staff=staff, clock_in=clock_in, clock_out=clock_out)


@my_bp.route('/overview', methods=['GET', 'POST'])
@require_staff_login
def overview():
    staff = _get_staff(session['staff_id'])
    prefs = attendance_unified.get_preferences(staff['staff_id'])

    if request.method == 'POST':
        hidden = request.form.getlist('hide')
        attendance_unified.save_preferences(staff['staff_id'], hidden)
        flash('表示設定を保存しました', 'success')
        return redirect(url_for('my.overview'))

    hidden_set = set(prefs.get('hidden_names') or [])
    all_status = attendance_unified.get_unified_status()
    grouped = {}
    for s in all_status:
        grouped.setdefault((s['company'], s['dept']), []).append(s)
    return render_template('my_overview.html', grouped=grouped, hidden_set=hidden_set)


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

    return render_template('my_schedule.html', staff=staff, today=datetime.now(JST).strftime('%Y-%m-%d'))
