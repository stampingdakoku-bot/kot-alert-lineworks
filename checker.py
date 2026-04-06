"""
kot-alert v2.3: 定刻アラーム＋打刻検知＋申請確認

フロー（出勤側 例: 13:00開始）:
  13:00  出勤アラーム（全員に1回）「出勤時間になりました。打刻をお願いします。」
  13:10  出勤打刻なし検知1回目
  13:20  出勤打刻なし検知2回目
  13:30〜 10分刻みで継続（最大4回）

フロー（退勤側 例: 22:00終了）:
  22:00  退勤アラーム（全員に1回）「お疲れさまでした。退勤時間になりました。」
  22:10  超過警告1回目
  22:20  超過警告2回目
  22:30〜 10分刻みで継続（最大4回）
  退勤打刻後: 乖離通知 → 申請リマインド（最大2回）

cron（毎時00分含む10分間隔 10:00〜23:00）:
  0,10,20,30,40,50 10-22 * * * cd /home/ubuntu/kot-alert && /usr/bin/python3 checker.py >> logs/cron.log 2>&1
  0 23 * * * cd /home/ubuntu/kot-alert && /usr/bin/python3 checker.py >> logs/cron.log 2>&1
"""
import sys
import os
import re
import logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import LOG_PATH, LW_DOMAIN_ID
import db_supabase as db
import kot_api
import lw_api
from lw_api import send_message, send_group_message
import requests

LW_GROUP_CHANNEL_ID = os.environ.get('LW_GROUP_CHANNEL_ID', '')

# --- ロギング設定 ---
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("checker")

JST = timezone(timedelta(hours=9))

# 店舗別カレンダー設定（Supabaseから取得）

MAX_OVERTIME_ALERTS = 4
MAX_LATE_CLOCKIN_ALERTS = 4
MAX_REQUEST_REMINDERS = 2



def get_calendar_events(store_name, store_info, today_str, headers):
    """店舗カレンダーからシフトイベントを取得"""
    uid = store_info["user_for_api"]
    cid = store_info["calendar_id"]
    from_dt = today_str + "T00:00:00+09:00"
    until_dt = today_str + "T23:59:59+09:00"
    url = (
        "https://www.worksapis.com/v1.0/users/" + uid
        + "/calendars/" + cid
        + "/events?fromDateTime=" + from_dt.replace("+", "%2B")
        + "&untilDateTime=" + until_dt.replace("+", "%2B")
        + "&count=100"
    )
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            return r.json().get("events", [])
        else:
            logger.warning("カレンダー取得失敗 %s: %d %s", store_name, r.status_code, r.text[:200])
            return []
    except Exception as e:
        logger.error("カレンダーAPI例外 %s: %s", store_name, str(e))
        return []


def parse_shift_name(summary):
    """カレンダーsummaryから名前を抽出: '13-22 内田' / '11:00-21:00 内田' → '内田'"""
    if not summary:
        return None
    m = re.match(r'^\d{1,2}(?::\d{2})?\s*[\-〜~]\s*\d{1,2}(?::\d{2})?\s*(.+)$', summary.strip())
    if m:
        name = m.group(1).strip()
        if name:
            return name
    return None


def build_name_to_employee_map(all_emps):
    """姓・姓名 → employee情報のマッピング"""
    name_map = {}
    for key, emp in all_emps.items():
        last_name = emp.get("last_name", "").strip()
        first_name = emp.get("first_name", "").strip()
        if last_name and last_name not in name_map:
            name_map[last_name] = emp
            name_map[last_name]["employee_key"] = key
        if last_name and first_name:
            full = last_name + first_name
            if full not in name_map:
                name_map[full] = {**emp, "employee_key": key}
            full_sp = last_name + " " + first_name
            if full_sp not in name_map:
                name_map[full_sp] = {**emp, "employee_key": key}
    return name_map

def get_timerecord_requests(today_str):
    """KOTから当月の打刻修正申請データを取得し、当日分をemployeeKeyでまとめて返す"""
    year_month = today_str[:7]
    try:
        data = kot_api._get("/requests/timerecords/" + year_month)
        if not data:
            return {}
    except Exception as e:
        logger.warning("打刻修正申請取得失敗: %s", str(e))
        return {}

    result = {}
    for req in data.get("requests", []):
        if req.get("date") == today_str:
            emp_key = req.get("employeeKey", "")
            status = req.get("status", "")
            if emp_key not in result:
                result[emp_key] = []
            result[emp_key].append(status)
    return result


def has_request_for_today(employee_key, timerecord_requests):
    """当日の打刻修正申請があるか"""
    statuses = timerecord_requests.get(employee_key, [])
    for s in statuses:
        if s in ("approved", "pending", "applying", "approvalProcess"):
            return True
    return False


def parse_shift_events(events, now, name_map, mappings):
    """カレンダーイベントをパースしてシフト情報リストを返す"""
    shifts = []
    for event in events:
        components = event.get("eventComponents", [])
        if not components:
            continue
        comp = components[0]
        summary = comp.get("summary", "")
        shift_name = parse_shift_name(summary)
        if not shift_name:
            continue

        start_info = comp.get("start", {})
        start_dt_str = start_info.get("dateTime", "")
        end_info = comp.get("end", {})
        end_dt_str = end_info.get("dateTime", "")
        if not start_dt_str or not end_dt_str:
            continue

        start_parsed = datetime.fromisoformat(start_dt_str)
        shift_start = now.replace(hour=start_parsed.hour, minute=start_parsed.minute, second=0, microsecond=0)
        end_parsed = datetime.fromisoformat(end_dt_str)
        shift_end = now.replace(hour=end_parsed.hour, minute=end_parsed.minute, second=0, microsecond=0)

        emp_info = name_map.get(shift_name)
        if not emp_info:
            continue

        emp_key = emp_info["employee_key"]
        lw_id = mappings.get(emp_key)
        if not lw_id:
            continue

        shifts.append({
            "shift_name": shift_name,
            "shift_start": shift_start,
            "shift_end": shift_end,
            "emp_key": emp_key,
            "emp_code": emp_info.get("employee_code", ""),
            "emp_name": emp_info.get("last_name", "") + " " + emp_info.get("first_name", ""),
            "lw_id": lw_id,
        })
    return shifts


def main():
    now = datetime.now(JST)
    today_str = now.strftime("%Y-%m-%d")

    logger.info("=" * 50)
    logger.info("kot-alert チェッカー開始 (v3.0 Supabase版)")
    logger.info("現在時刻: %s", now.strftime("%H:%M"))
    logger.info("対象日: %s", today_str)

    # LINE WORKS アクセストークン取得
    lw_api._token_cache["access_token"] = None
    lw_api._token_cache["expires_at"] = 0
    token = lw_api.get_access_token()
    if not token:
        logger.error("LINE WORKSトークン取得失敗")
        return
    headers = {"Authorization": "Bearer " + token}

    # 従業員マスタ
    all_emps = {e["employee_key"]: e for e in db.get_all_employees()}
    name_map = build_name_to_employee_map(all_emps)

    # マッピング（employee_key → lw_account_id）
    mappings_list = db.get_all_mappings()
    mappings = {m["employee_key"]: m["lw_account_id"] for m in mappings_list}

    # アラート設定をDBから取得
    settings = db.get_alert_settings()

    # アラート文言テンプレートをDBから取得
    alert_templates = db.get_alert_templates()
    MAX_LATE_CLOCKIN_ALERTS = settings.get('late_clockin_max_count', 4)
    MAX_OVERTIME_ALERTS = settings.get('overtime_max_count', 4)
    MAX_REQUEST_REMINDERS = settings.get('request_reminder_max_count', 2)
    ADMIN_LW_ID = settings.get('admin_lw_id', 'sakamoto.tatsuya@avivastarscorporation')

    clockin_alarm_sent = 0
    clockout_alarm_sent = 0
    late_clockin_notified = 0
    overtime_notified = 0
    deviation_notified = 0
    reminder_notified = 0
    matched = 0

    # 店舗カレンダー設定をSupabaseから取得
    STORE_CALENDARS = db.get_store_calendars()
    logger.info("店舗設定: %d店舗", len(STORE_CALENDARS))

    # カレンダーイベントを一括取得（APIコール節約）
    all_store_shifts = {}
    for store_name, store_info in STORE_CALENDARS.items():
        events = get_calendar_events(store_name, store_info, today_str, headers)
        logger.info("%s: %d件のシフト", store_name, len(events))
        all_store_shifts[store_name] = parse_shift_events(events, now, name_map, mappings)

    # ========================================
    # フェーズ1: 定刻アラーム（KOTデータ不要）
    # ========================================
    for store_name, shifts in all_store_shifts.items():
        for s in shifts:
            matched += 1
            shift_start = s["shift_start"]
            shift_end = s["shift_end"]
            emp_key = s["emp_key"]
            emp_code = s["emp_code"]
            emp_name = s["emp_name"]
            lw_id = s["lw_id"]
            shift_start_str = shift_start.strftime("%H:%M")
            shift_end_str = shift_end.strftime("%H:%M")

            # === 出勤アラーム ===
            # シフト開始時刻 〜 +10分の間（1回のみ）
            if settings.get('clockin_alarm_enabled', True) and shift_start <= now < shift_start + timedelta(minutes=10):
                if not db.was_alert_sent(emp_key, "clockin_alarm", today_str):
                    tmpl = alert_templates.get('clockin_alarm',
                        '🔔 出勤時間になりました（{shift_start}）\n打刻をお願いします。')
                    message = tmpl.format(shift_start=shift_start_str, shift_end=shift_end_str,
                                          count='', clock_out='', diff='')
                    if lw_api.send_group_message(LW_GROUP_CHANNEL_ID, message):
                        db.record_alert(emp_key, "clockin_alarm", today_str, message)
                        clockin_alarm_sent += 1
                        logger.info("出勤アラーム: %s %s (%s, %s)",
                                    emp_code, emp_name, store_name, shift_start_str)

            # === 退勤アラーム ===
            # シフト終了時刻 〜 +10分の間（1回のみ）
            if settings.get('clockout_alarm_enabled', True) and shift_end <= now < shift_end + timedelta(minutes=10):
                if not db.was_alert_sent(emp_key, "clockout_alarm", today_str):
                    tmpl = alert_templates.get('clockout_alarm',
                        '🔔 お疲れさまでした。\n退勤時間になりました（{shift_end}）\n打刻して速やかにお帰りください。')
                    message = tmpl.format(shift_start=shift_start_str, shift_end=shift_end_str,
                                          count='', clock_out='', diff='')
                    if lw_api.send_group_message(LW_GROUP_CHANNEL_ID, message):
                        db.record_alert(emp_key, "clockout_alarm", today_str, message)
                        clockout_alarm_sent += 1
                        logger.info("退勤アラーム: %s %s (%s, %s)",
                                    emp_code, emp_name, store_name, shift_end_str)

    # ========================================
    # フェーズ2: 打刻チェック（KOTデータ必要）
    # ========================================
    if kot_api.is_api_blocked():
        logger.info("KOT API利用禁止時間帯のため打刻チェックスキップ")
        logger.info("出勤アラーム: %d名, 退勤アラーム: %d名",
                    clockin_alarm_sent, clockout_alarm_sent)
        logger.info("=" * 50)
        return

    # KOT打刻データ取得
    raw_timerecords = kot_api.get_timerecords(today_str)
    timerecords = kot_api.parse_timerecords_for_employee(raw_timerecords)
    logger.info("打刻データ: %d件", len(timerecords))

    # KOT申請データ取得（当日分）
    tr_requests = get_timerecord_requests(today_str)
    logger.info("当日申請データ: %d名", len(tr_requests))

    checked = 0

    for store_name, shifts in all_store_shifts.items():
        for s in shifts:
            shift_start = s["shift_start"]
            shift_end = s["shift_end"]
            emp_key = s["emp_key"]
            emp_code = s["emp_code"]
            emp_name = s["emp_name"]
            lw_id = s["lw_id"]
            shift_start_str = shift_start.strftime("%H:%M")
            shift_end_str = shift_end.strftime("%H:%M")

            # KOT打刻データ確認
            tr = timerecords.get(emp_key, {})
            has_clock_in = tr.get("clock_in") is not None
            has_clock_out = tr.get("clock_out") is not None

            # === 出勤打刻なし検知（シフト開始+10分〜） ===
            if settings.get('late_clockin_enabled', True) and now >= shift_start + timedelta(minutes=10) and not has_clock_in:
                alert_count = db.count_alerts_today(emp_key, "late_clockin", today_str)
                if alert_count < MAX_LATE_CLOCKIN_ALERTS:
                    round_num = alert_count + 1
                    tmpl = alert_templates.get('late_clockin',
                        '⚠️ 出勤打刻の確認（{count}回目）\nシフト開始時刻（{shift_start}）を過ぎましたが、出勤打刻が確認できません。\n打刻漏れはないですか？\n確認をお願いします。')
                    message = tmpl.format(shift_start=shift_start_str, shift_end=shift_end_str,
                                          count=str(round_num), clock_out='', diff='')
                    if lw_api.send_group_message(LW_GROUP_CHANNEL_ID, message):
                        db.record_alert(emp_key, "late_clockin", today_str, message)
                        late_clockin_notified += 1
                        logger.info("出勤打刻なし(%d回目): %s %s (%s, シフト開始%s)",
                                    round_num, emp_code, emp_name, store_name, shift_start_str)
                continue  # 出勤打刻なしなら退勤チェック不要

            # 出勤打刻なし（シフト開始前 or +10分前）→ スキップ
            if not has_clock_in:
                continue

            checked += 1

            # === 退勤チェック（シフト終了+10分を過ぎている場合のみ） ===
            if now < shift_end + timedelta(minutes=10):
                continue

            if not has_clock_out and settings.get('overtime_enabled', True):
                # === 超過警告 ===
                alert_count = db.count_alerts_today(emp_key, "overtime", today_str)
                if alert_count >= MAX_OVERTIME_ALERTS:
                    logger.debug("%s %s: 超過警告上限到達", emp_code, emp_name)
                else:
                    round_num = alert_count + 1
                    tmpl = alert_templates.get('overtime',
                        '⚠️ まだ退勤打刻がありません。\nKOT打刻申請から申請してください。\nタイムカード → 該当日の詳細 → 打刻申請 → 新規 → 申請メッセージ入力 → 申請')
                    message = tmpl.format(shift_start=shift_start_str, shift_end=shift_end_str,
                                          count=str(round_num), clock_out='', diff='')

                    if lw_api.send_group_message(LW_GROUP_CHANNEL_ID, message):
                        db.record_alert(emp_key, "overtime", today_str, message)
                        overtime_notified += 1
                        logger.info("超過警告(%d回目): %s %s (%s, シフト終了%s)",
                                    round_num, emp_code, emp_name, store_name, shift_end_str)

            elif has_clock_out:
                # === 退勤打刻あり: 乖離通知（従来どおり） ===
                diff_minutes = int((tr["clock_out"] - shift_end).total_seconds() / 60)
                clock_out_str = tr["clock_out"].strftime("%H:%M")

                if diff_minutes > 1 and not has_request_for_today(emp_key, tr_requests):
                    if settings.get('deviation_enabled', True) and not db.was_alert_sent(emp_key, "deviation", today_str):
                        tmpl = alert_templates.get('deviation',
                            '📋 勤務時間のお知らせ\nシフト終了: {shift_end}\n退勤打刻: {clock_out}（{diff}分超過）\n修正申請がまだ提出されていません。\nお早めに申請をお願いします。')
                        message = tmpl.format(shift_start=shift_start_str, shift_end=shift_end_str,
                                              count='', clock_out=clock_out_str, diff=str(diff_minutes))

                        if lw_api.send_group_message(LW_GROUP_CHANNEL_ID, message):
                            db.record_alert(emp_key, "deviation", today_str, message)
                            deviation_notified += 1
                            logger.info("乖離通知: %s %s (%s, シフト%s, 退勤%s)",
                                        emp_code, emp_name, store_name, shift_end_str, clock_out_str)

            # === 申請リマインド（新ロジック） ===
            # パターンA: 退勤打刻なし＆overtime開始分数経過
            # パターンB: 退勤打刻あり＆15分以上超過
            if settings.get('request_reminder_enabled', True):
                overtime_start_min = settings.get('overtime_start_minutes', 10)
                reminder_interval = settings.get('request_reminder_interval_minutes', 10)
                send_reminder = False

                if not has_clock_out and now >= shift_end + timedelta(minutes=overtime_start_min):
                    # パターンA: 退勤打刻なし
                    if not has_request_for_today(emp_key, tr_requests):
                        send_reminder = True
                elif has_clock_out:
                    # パターンB: 退勤打刻あり＆15分以上超過
                    diff_min = int((tr["clock_out"] - shift_end).total_seconds() / 60)
                    if diff_min >= 15 and not has_request_for_today(emp_key, tr_requests):
                        send_reminder = True

                if send_reminder:
                    reminder_count = db.count_alerts_today(emp_key, "request_reminder", today_str)
                    if reminder_count < MAX_REQUEST_REMINDERS:
                        # overtimeアラートとの重複防止: 最終overtime送信から設定間隔分後まで待つ
                        last_overtime = db.get_last_alert_time(emp_key, "overtime", today_str)
                        if last_overtime and (now - last_overtime).total_seconds() < reminder_interval * 60:
                            logger.debug("%s %s: overtime送信直後のためリマインドスキップ", emp_code, emp_name)
                        else:
                            # 前回のrequest_reminderからも間隔を空ける
                            last_reminder = db.get_last_alert_time(emp_key, "request_reminder", today_str)
                            if last_reminder and (now - last_reminder).total_seconds() < reminder_interval * 60:
                                logger.debug("%s %s: リマインド間隔未到達", emp_code, emp_name)
                            else:
                                round_num = reminder_count + 1
                                tmpl = alert_templates.get('request_reminder',
                                    '📋 打刻申請がまだ完了していません。\nKOTから申請をお願いします。\nタイムカード → 該当日の詳細 → 打刻申請 → 新規（または編集） → 申請メッセージ入力 → 申請')
                                message = tmpl.format(shift_start=shift_start_str, shift_end=shift_end_str,
                                                      count=str(round_num), clock_out='', diff='')

                                if lw_api.send_group_message(LW_GROUP_CHANNEL_ID, message):
                                    db.record_alert(emp_key, "request_reminder", today_str, message)
                                    reminder_notified += 1
                                    logger.info("申請リマインド(%d回目): %s %s (%s)",
                                                round_num, emp_code, emp_name, store_name)

    logger.info("出勤アラーム: %d名, 退勤アラーム: %d名, 出勤なし: %d名, 超過: %d名, 乖離: %d名, リマインド: %d名",
                clockin_alarm_sent, clockout_alarm_sent, late_clockin_notified,
                overtime_notified, deviation_notified, reminder_notified)

    # ========================================
    # 13:10 前日勤怠まとめ（グループ送信）
    # ========================================
    report_hour = 13
    report_minute = 10
    if now.hour == report_hour and report_minute <= now.minute < report_minute + 10:
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        if not db.was_alert_sent("__admin__", "morning_check", yesterday_str):
            send_daily_report(all_emps, yesterday_str)

    logger.info("=" * 50)


# 管理者LW ID
ADMIN_LW_ID = "sakamoto.tatsuya@avivastarscorporation"


def send_daily_report(all_emps=None, yesterday_str=None):
    """前日の勤怠まとめを13:10にグループ送信する"""
    if yesterday_str is None:
        yesterday_str = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")
    if all_emps is None:
        all_emps = {e["employee_key"]: e for e in db.get_all_employees()}

    # 前日のアラート集計
    result = db.supabase.table('alerts_sent').select('employee_key, flow_type') \
        .eq('alert_date', yesterday_str).execute()

    counts = {}
    problem_names = {}
    for row in result.data:
        ft = row["flow_type"]
        ek = row["employee_key"]
        if ek == "__admin__":
            continue
        # 重複排除（同一人物が複数回アラートされた場合）
        key = (ft, ek)
        if ft not in problem_names:
            problem_names[ft] = set()
        if ek in problem_names[ft]:
            continue
        problem_names[ft].add(ek)
        counts[ft] = counts.get(ft, 0) + 1

    # KOT APIから前日の退勤打刻時刻を取得
    clockout_map = {}
    try:
        daily_data = kot_api.get_timerecords(yesterday_str)
        workings = []
        if isinstance(daily_data, dict):
            workings = daily_data.get("dailyWorkings", [])
        elif isinstance(daily_data, list):
            for d in daily_data:
                if isinstance(d, dict):
                    workings.extend(d.get("dailyWorkings", []) or [d])
        for w in workings:
            ek = w.get("employeeKey", "")
            if not ek:
                continue
            records = w.get("timeRecord", [])
            if isinstance(records, dict):
                records = [records]
            latest_out = None
            for tr in records:
                if str(tr.get("code", "")) == "2":
                    t = tr.get("time", "")
                    if t:
                        try:
                            dt = datetime.fromisoformat(t)
                            if latest_out is None or dt > latest_out:
                                latest_out = dt
                        except ValueError:
                            pass
            if latest_out is not None:
                clockout_map[ek] = latest_out.strftime("%H:%M")
    except Exception as e:
        logger.warning("前日打刻データ取得失敗: %s", str(e))

    def _name(ek):
        emp = all_emps.get(ek, {})
        return (emp.get("last_name", "") + emp.get("first_name", "")).strip() or "?"

    def _name_with_clockout(ek):
        nm = _name(ek)
        if ek in clockout_map:
            return nm + " 退勤" + clockout_map[ek]
        return nm + " 退勤なし"

    def _names_with_clockout(ft):
        eks = problem_names.get(ft, set())
        return "、".join(sorted(_name_with_clockout(ek) for ek in eks))

    def _names(ft):
        eks = problem_names.get(ft, set())
        return "、".join(sorted(_name(ek) for ek in eks))

    lines = ["📊 前日勤怠まとめ（" + yesterday_str + "）", ""]

    for ft, label in [("late_clockin", "出勤打刻なし"), ("overtime", "超過警告")]:
        cnt = counts.get(ft, 0)
        if cnt > 0:
            lines.append(label + ": " + str(cnt) + "件（" + _names_with_clockout(ft) + "）")
        else:
            lines.append(label + ": 0件")

    dev_cnt = counts.get("deviation", 0)
    if dev_cnt > 0:
        lines.append("乖離通知: " + str(dev_cnt) + "件（" + _names("deviation") + "）")
    else:
        lines.append("乖離通知: 0件")

    # 申請漏れ情報（late_clockin + overtime + deviation 対象者の申請有無）
    lines.append("")
    target_eks = set()
    for ft in ("late_clockin", "overtime", "deviation"):
        target_eks |= problem_names.get(ft, set())
    target_eks = list(target_eks)

    if target_eks:
        tr_requests = get_timerecord_requests(yesterday_str)
        no_request = []
        has_request = []
        for ek in target_eks:
            nm = _name(ek)
            if has_request_for_today(ek, tr_requests):
                has_request.append(nm)
            else:
                no_request.append(nm)
        if no_request:
            lines.append("❌ 申請漏れ: " + "、".join(sorted(no_request)))
        if has_request:
            lines.append("✅ 申請済: " + "、".join(sorted(has_request)))
        if not no_request:
            lines.append("全員申請済みです。")
    else:
        lines.append("対象者なし。申請チェック不要です。")

    lines.append("")
    lines.append("⚙️ 申請漏れがある場合はKING OF TIMEより修正を行ってください。")
    lines.append("")
    lines.append("▶ 管理画面")
    lines.append("http://133.125.93.39/")

    message = "\n".join(lines)
    if lw_api.send_group_message(LW_GROUP_CHANNEL_ID, message):
        # flow_typeは既存check制約に合わせて morning_check を流用
        db.record_alert("__admin__", "morning_check", yesterday_str, message)
        logger.info("前日勤怠まとめ送信完了")
    else:
        logger.error("前日勤怠まとめ送信失敗")
    return message


def send_nightly_report(today_str, all_emps, admin_lw_id=None):
    """速報: 打刻ベースの出退勤状況・乖離のみ"""
    result = db.supabase.table('alerts_sent').select('employee_key, flow_type').eq('alert_date', today_str).execute()

    # flow_type別に集計
    counts = {}
    problem_names = {}
    for row in result.data:
        ft = row["flow_type"]
        ek = row["employee_key"]
        counts[ft] = counts.get(ft, 0) + 1
        emp = all_emps.get(ek, {})
        name = emp.get("last_name", "?")
        if ft not in problem_names:
            problem_names[ft] = set()
        problem_names[ft].add(name)

    # 打刻ベースの項目のみ（申請関連を除外）
    flow_labels = {
        "clockin_alarm": "出勤アラーム",
        "clockout_alarm": "退勤アラーム",
        "late_clockin": "出勤打刻なし",
        "overtime": "超過警告",
        "deviation": "乖離通知",
    }

    has_problem = any(counts.get(ft, 0) > 0 for ft in ("late_clockin", "overtime", "deviation"))

    lines = ["📊 本日の勤怠速報（" + today_str + "）\n"]

    for ft, label in flow_labels.items():
        cnt = counts.get(ft, 0)
        if cnt > 0:
            names = "、".join(sorted(problem_names.get(ft, set())))
            if ft in ("clockin_alarm", "clockout_alarm"):
                lines.append(label + ": " + str(cnt) + "件")
            else:
                lines.append(label + ": " + str(cnt) + "件（" + names + "）")
        else:
            lines.append(label + ": 0件")

    if not has_problem:
        lines.append("\n全員正常に打刻されました。")
    else:
        lines.append("\n※申請状況は明朝チェックします。")

    message = "\n".join(lines)
    _admin = admin_lw_id or ADMIN_LW_ID
    if lw_api.send_group_message(LW_GROUP_CHANNEL_ID, message):
        logger.info("速報送信完了")
    else:
        logger.error("速報送信失敗")


def send_morning_request_check(yesterday_str, all_emps, admin_lw_id=None):
    """翌朝: 前日のシフト超過者で申請未提出の人をチェック"""
    # 既に送信済みならスキップ（1日1回）
    already = db.supabase.table('alerts_sent').select('id', count='exact') \
        .eq('employee_key', '__admin__').eq('flow_type', 'morning_check') \
        .eq('alert_date', yesterday_str).execute()
    if already.count and already.count > 0:
        logger.info("翌朝チェック: %s分は送信済み", yesterday_str)
        return

    # 前日に乖離通知を受けた人を取得
    deviation_result = db.supabase.table('alerts_sent').select('employee_key') \
        .eq('alert_date', yesterday_str).eq('flow_type', 'deviation').execute()
    seen = set()
    deviation_rows = []
    for row in deviation_result.data:
        ek = row['employee_key']
        if ek not in seen:
            seen.add(ek)
            deviation_rows.append(row)

    if not deviation_rows:
        message = (
            "📋 前日の申請チェック（" + yesterday_str + "）\n\n"
            + "シフト超過者なし。申請チェック不要です。"
        )
        _admin = admin_lw_id or ADMIN_LW_ID
        lw_api.send_group_message(LW_GROUP_CHANNEL_ID, message)
        # 送信済み記録
        db.record_alert("__admin__", "morning_check", yesterday_str, "no_deviation")
        logger.info("翌朝チェック送信（超過者なし）")
        return

    # KOT申請データ取得
    tr_requests = get_timerecord_requests(yesterday_str)

    no_request = []
    has_request = []
    for row in deviation_rows:
        ek = row["employee_key"]
        emp = all_emps.get(ek, {})
        name = emp.get("last_name", "?") + " " + emp.get("first_name", "")
        if has_request_for_today(ek, tr_requests):
            has_request.append(name)
        else:
            no_request.append(name)

    lines = ["📋 前日の申請チェック（" + yesterday_str + "）\n"]

    if no_request:
        lines.append("❌ 未申請: " + "、".join(no_request))
    if has_request:
        lines.append("✅ 申請済: " + "、".join(has_request))
    if not no_request:
        lines.append("\n全員申請済みです。")

    message = "\n".join(lines)
    _admin = admin_lw_id or ADMIN_LW_ID
    if lw_api.send_group_message(LW_GROUP_CHANNEL_ID, message):
        db.record_alert("__admin__", "morning_check", yesterday_str, message)
        logger.info("翌朝チェック送信完了（未申請: %d名, 申請済: %d名）", len(no_request), len(has_request))
    else:
        logger.error("翌朝チェック送信失敗")


if __name__ == "__main__":
    main()
