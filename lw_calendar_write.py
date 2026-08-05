"""
LINE WORKSカレンダーへの予定作成。既存neesa_lw.pyの読み取り専用サービスアカウント
(JWT)を流用し、書き込み時のみ scope=calendar のトークンを別途取得する
(scope=calendar.readのトークンでは書き込み不可、Phase 0スパイクで検証済み)。
"""
import re
import time
import logging
from datetime import datetime, timedelta, timezone

import requests
import jwt
from dateutil import rrule as _rrule
from dateutil.parser import isoparse as _isoparse

import neesa_lw

logger = logging.getLogger(__name__)

# 予定登録のデフォルト先カレンダー(合同会社NeeSa。/board からも参照される)
DEFAULT_CALENDAR_ID = "b6cc3c42-23e0-462c-a5e7-ca3c272f12bc"


def _get_write_token():
    now = int(time.time())
    payload = {
        "iss": neesa_lw.CLIENT_ID,
        "sub": neesa_lw.SERVICE_ACCOUNT,
        "iat": now,
        "exp": now + 3600,
    }
    with open(neesa_lw.PRIVATE_KEY_PATH, "r") as f:
        private_key = f.read()
    assertion = jwt.encode(payload, private_key, algorithm="RS256")
    data = {
        "assertion": assertion,
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id": neesa_lw.CLIENT_ID,
        "client_secret": neesa_lw.CLIENT_SECRET,
        "scope": "calendar",
    }
    resp = requests.post(neesa_lw.AUTH_URL, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def create_event(summary, start_dt, end_dt, calendar_id=None, user_id=None, recurrence=None,
                  attendees=None, reminders=None):
    """予定を作成する。recurrence指定時は繰り返し予定として登録。戻り値: (success: bool, detail)"""
    calendar_id = calendar_id or DEFAULT_CALENDAR_ID
    user_id = user_id or neesa_lw.DEFAULT_USER
    try:
        token = _get_write_token()
    except Exception as e:
        logger.error("カレンダー書き込みトークン取得失敗: %s", e)
        return False, str(e)

    url = f"{neesa_lw.API_BASE}/users/{user_id}/calendars/{calendar_id}/events"
    comp = {
        "summary": summary,
        "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tokyo"},
    }
    if recurrence:
        comp["recurrence"] = recurrence
    if attendees:
        comp["attendees"] = attendees
    if reminders:
        comp["reminders"] = reminders
    body = {"eventComponents": [comp], "sendNotification": False}
    try:
        resp = requests.post(
            url, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            json=body, timeout=30,
        )
        if resp.status_code in (200, 201):
            return True, resp.json()
        logger.warning("予定作成失敗 status=%s body=%s", resp.status_code, resp.text[:500])
        return False, resp.text
    except Exception as e:
        logger.error("予定作成エラー: %s", e)
        return False, str(e)


def _events_url(event_id, calendar_id, user_id):
    calendar_id = calendar_id or DEFAULT_CALENDAR_ID
    user_id = user_id or neesa_lw.DEFAULT_USER
    return f"{neesa_lw.API_BASE}/users/{user_id}/calendars/{calendar_id}/events/{event_id}"


def update_event(event_id, summary, start_dt, end_dt, recurrence=None, calendar_id=None, user_id=None,
                  attendees=None, reminders=None):
    """予定を更新する(全体入れ替え)。戻り値: (success: bool, detail)"""
    try:
        token = _get_write_token()
    except Exception as e:
        logger.error("カレンダー書き込みトークン取得失敗: %s", e)
        return False, str(e)

    comp = {
        "eventId": event_id,
        "summary": summary,
        "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tokyo"},
    }
    if recurrence:
        comp["recurrence"] = recurrence
    if attendees:
        comp["attendees"] = attendees
    if reminders:
        comp["reminders"] = reminders
    body = {"eventComponents": [comp], "sendNotification": False}
    try:
        resp = requests.put(
            _events_url(event_id, calendar_id, user_id),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            json=body, timeout=30,
        )
        if resp.status_code in (200, 204):
            return True, (resp.json() if resp.text else {})
        logger.warning("予定更新失敗 status=%s body=%s", resp.status_code, resp.text[:500])
        return False, resp.text
    except Exception as e:
        logger.error("予定更新エラー: %s", e)
        return False, str(e)


def delete_event(event_id, calendar_id=None, user_id=None):
    """予定(シリーズ全体)を削除する。単一occurrenceの削除はEXDATE追加(update_event)で行う。"""
    try:
        token = _get_write_token()
    except Exception as e:
        logger.error("カレンダー書き込みトークン取得失敗: %s", e)
        return False, str(e)
    try:
        resp = requests.delete(
            _events_url(event_id, calendar_id, user_id),
            headers={"Authorization": "Bearer " + token},
            params={"sendNotification": "false"}, timeout=30,
        )
        if resp.status_code in (200, 204):
            return True, None
        logger.warning("予定削除失敗 status=%s body=%s", resp.status_code, resp.text[:500])
        return False, resp.text
    except Exception as e:
        logger.error("予定削除エラー: %s", e)
        return False, str(e)


def _get_events_raw(from_dt, until_dt, calendar_id=None, user_id=None):
    """eventId・recurrenceを保持したまま取得(編集・削除用。neesa_lw.get_calendar_eventsは
    eventIdを捨てて平坦化するため、こちらは専用に実装して既存関数には触れない)。"""
    calendar_id = calendar_id or DEFAULT_CALENDAR_ID
    user_id = user_id or neesa_lw.DEFAULT_USER
    token = neesa_lw.get_access_token()
    if not token:
        return []
    url = f"{neesa_lw.API_BASE}/users/{user_id}/calendars/{calendar_id}/events"
    headers = {"Authorization": "Bearer " + token}
    params = {"fromDateTime": from_dt, "untilDateTime": until_dt}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("events", [])
    except Exception as e:
        logger.error("イベント取得失敗: %s", e)
        return []


def _occurs_on(comp, target_date):
    """comp(eventComponent)がtarget_dateに出現するか判定する。

    neesa_lw._applies_on と同種の判定だが、こちらは全て内部でUTC-naiveに揃えて
    dateutil.rruleに渡す。理由: LINE WORKS側はUNTILを保存すると必ず末尾にZ(UTC)を
    付けて返すため、dtstart側がnaive(JSTのつもりの素の時刻)のままだと
    dateutilが「naiveとawareの比較」で例外を出し、_applies_on側のtry/exceptで
    黙って握りつぶされてRRULEが完全に無視される(＝DTSTART当日にしか一致しなくなる)
    不具合が生じることを確認したため、専用に実装している。"""
    start = comp.get("start", {})
    sdt = start.get("dateTime")
    if not sdt:
        return False
    try:
        dtstart_naive = _isoparse(sdt).replace(tzinfo=None)
    except (ValueError, TypeError):
        return False
    dtstart_utc = dtstart_naive.replace(tzinfo=neesa_lw.JST).astimezone(timezone.utc).replace(tzinfo=None)

    rec = comp.get("recurrence") or []
    if not rec:
        return dtstart_naive.date() == target_date

    rset = _rrule.rruleset()
    has_rule = False
    for line in rec:
        if line.startswith("RRULE:"):
            rule_str = re.sub(r'UNTIL=(\d{8}T\d{6})Z', r'UNTIL=\1', line[len("RRULE:"):])
            try:
                rset.rrule(_rrule.rrulestr(rule_str, dtstart=dtstart_utc))
                has_rule = True
            except (ValueError, TypeError):
                pass
        elif line.startswith("EXDATE"):
            val = line.split(":", 1)[-1]
            for d in val.split(","):
                d = d.strip().rstrip("Z")
                try:
                    exdt_naive = datetime.strptime(d, "%Y%m%dT%H%M%S")
                    exdt_utc = exdt_naive.replace(tzinfo=neesa_lw.JST).astimezone(timezone.utc).replace(tzinfo=None)
                    rset.exdate(exdt_utc)
                except ValueError:
                    pass
    if not has_rule:
        return dtstart_naive.date() == target_date

    day0_jst = datetime(target_date.year, target_date.month, target_date.day, tzinfo=neesa_lw.JST)
    window_start = (day0_jst - timedelta(hours=1)).astimezone(timezone.utc).replace(tzinfo=None)
    window_end = (day0_jst + timedelta(days=1, hours=1)).astimezone(timezone.utc).replace(tzinfo=None)
    for occ in rset.between(window_start, window_end, inc=True):
        occ_jst_date = occ.replace(tzinfo=timezone.utc).astimezone(neesa_lw.JST).date()
        if occ_jst_date == target_date:
            return True
    return False


def get_my_events(display_name, start_date, end_date, calendar_id=None, user_id=None):
    """summaryにdisplay_nameを含む予定をeventId付きで返す(本人の予定の閲覧・編集・削除用)。
    戻り値: {date_iso: [{event_id, summary, start_time, end_time, recurrence}, ...]}"""
    base = datetime(start_date.year, start_date.month, start_date.day, tzinfo=neesa_lw.JST)
    frm = base.isoformat()
    unt = (datetime(end_date.year, end_date.month, end_date.day, tzinfo=neesa_lw.JST) + timedelta(days=1)).isoformat()
    raw_events = _get_events_raw(frm, unt, calendar_id, user_id)

    result = {}
    d = start_date
    while d <= end_date:
        result[d.isoformat()] = []
        d += timedelta(days=1)

    for e in raw_events:
        for comp in e.get("eventComponents", []):
            event_id = comp.get("eventId")
            summary = comp.get("summary", "")
            if display_name not in summary:
                continue
            start = comp.get("start", {})
            sdt = start.get("dateTime")
            if not sdt:
                continue
            end = comp.get("end", {})
            edt = end.get("dateTime", "")
            try:
                dtstart = neesa_lw.isoparse(sdt).replace(tzinfo=None)
                dtend = neesa_lw.isoparse(edt).replace(tzinfo=None) if edt else dtstart
            except (ValueError, TypeError):
                continue
            recurrence = comp.get("recurrence") or []
            attendees = comp.get("attendees") or []
            reminders = comp.get("reminders") or []
            d = start_date
            while d <= end_date:
                if _occurs_on(comp, d):
                    result[d.isoformat()].append({
                        'event_id': event_id,
                        'summary': summary,
                        'start_time': f'{dtstart.hour:02d}:{dtstart.minute:02d}',
                        'end_time': f'{dtend.hour:02d}:{dtend.minute:02d}',
                        'is_recurring': bool(recurrence),
                        'recurrence': recurrence,
                        'attendees': attendees,
                        'reminders': reminders,
                        'occurrence_date': d.isoformat(),
                        'series_start': dtstart.isoformat(),
                        'series_end': dtend.isoformat(),
                    })
                d += timedelta(days=1)
    return result


def get_my_events_all(display_name, start_date, end_date):
    """個人の予定登録用カレンダー(DEFAULT_CALENDAR_ID)に加え、@121チームのシフト実績
    カレンダー(neesa_lw.AT121_CALENDAR_ID)も対象に本人名を含む予定をまとめて返す。
    松田のように@121専用カレンダーでシフトが管理されているスタッフは、本人名一致の
    シフトがDEFAULT_CALENDAR_ID側に存在しないため、get_my_events単体では見つからず
    /shifts等の本人予定クリック編集が「見つかりませんでした」になってしまう。
    各イベントにcalendar_idを付与し、編集・削除時にどちらのカレンダーへ書き込むか
    呼び出し側で判別できるようにする。"""
    merged = get_my_events(display_name, start_date, end_date)
    for evs in merged.values():
        for ev in evs:
            ev['calendar_id'] = DEFAULT_CALENDAR_ID
    at121 = get_my_events(display_name, start_date, end_date, calendar_id=neesa_lw.AT121_CALENDAR_ID)
    for d, evs in at121.items():
        for ev in evs:
            ev['calendar_id'] = neesa_lw.AT121_CALENDAR_ID
        merged.setdefault(d, []).extend(evs)
    return merged


def build_rrule(freq, weekdays):
    """freq: 'weekly' | 'monthly', weekdays: ['MO','TU',...] のRRULE文字列を1本組み立てる"""
    freq_code = 'WEEKLY' if freq == 'weekly' else 'MONTHLY'
    byday = ','.join(weekdays)
    return f"RRULE:FREQ={freq_code};BYDAY={byday}"


def _rrule_with_until(rrule_line, until_dt_utc):
    """既存のRRULE行のUNTILを置き換える(なければ追加)。until_dt_utcはtz-aware UTC datetime"""
    until_str = until_dt_utc.strftime("%Y%m%dT%H%M%SZ")
    parts = rrule_line[len("RRULE:"):].split(';')
    parts = [p for p in parts if not p.startswith('UNTIL=')]
    parts.append(f'UNTIL={until_str}')
    return "RRULE:" + ";".join(parts)


def truncate_recurrence_before(recurrence, occurrence_date):
    """occurrence_date(この日)以降を除外するようRRULEにUNTILを設定した新しいrecurrenceを返す"""
    cutoff_local = datetime(occurrence_date.year, occurrence_date.month, occurrence_date.day,
                             0, 0, 0, tzinfo=neesa_lw.JST) - timedelta(seconds=1)
    cutoff_utc = cutoff_local.astimezone(timezone.utc)
    new_rec = []
    for line in recurrence:
        if line.startswith('RRULE:'):
            new_rec.append(_rrule_with_until(line, cutoff_utc))
        else:
            new_rec.append(line)
    return new_rec


def exclude_occurrence(recurrence, occurrence_dt_local):
    """occurrence_dt_local(該当occurrenceの開始日時, tz無し/JSTのnaive datetime)をEXDATEとして
    recurrenceに追加した新しいrecurrenceを返す"""
    exdate_str = occurrence_dt_local.strftime("%Y%m%dT%H%M%S")
    new_rec = list(recurrence)
    new_rec.append(f"EXDATE;TZID=Asia/Tokyo:{exdate_str}")
    return new_rec


def remap_exdate_times(recurrence, new_time):
    """EXDATE行の時刻部分を new_time(datetime.time) に置き換える。
    編集で開始時刻を変更すると、既存のEXDATE(除外日時)が新時刻と一致せず
    無効化されてしまい、削除したはずのoccurrenceが復活してしまうため、
    編集時は必ずこれを通してから update_event に渡す。"""
    new_hms = new_time.strftime("%H%M%S")
    out = []
    for line in recurrence:
        if line.startswith("EXDATE"):
            prefix, _, val = line.partition(":")
            new_vals = []
            for d in val.split(","):
                d = d.strip()
                date_part = d[:8]
                new_vals.append(f"{date_part}T{new_hms}")
            out.append(f"{prefix}:{','.join(new_vals)}")
        else:
            out.append(line)
    return out


def list_upcoming_events(days=14, calendar_id=None, user_id=None):
    """今日から指定日数分の予定を、開始日時順に並べて返す(既存の読み取り専用
    calendar.readトークンを使う neesa_lw.get_calendar_events を流用)。"""
    calendar_id = calendar_id or DEFAULT_CALENDAR_ID
    user_id = user_id or neesa_lw.DEFAULT_USER
    base = datetime.now(neesa_lw.JST).replace(hour=0, minute=0, second=0, microsecond=0)
    frm = base.isoformat()
    unt = (base + timedelta(days=days)).isoformat()

    events = neesa_lw.get_calendar_events(calendar_id, frm, unt, user_id=user_id)
    out = []
    for e in events:
        start = e.get("start", {}) or {}
        end = e.get("end", {}) or {}
        dt = start.get("dateTime") or start.get("date")
        if not dt:
            continue
        out.append({
            "summary": e.get("summary", ""),
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "all_day": "dateTime" not in start,
        })
    out.sort(key=lambda x: x["start"])
    return out


def list_upcoming_events(days=14, calendar_id=None, user_id=None):
    """今日から指定日数分の予定を、開始日時順に並べて返す(既存の読み取り専用
    calendar.readトークンを使う neesa_lw.get_calendar_events を流用)。"""
    from datetime import datetime, timedelta

    calendar_id = calendar_id or DEFAULT_CALENDAR_ID
    user_id = user_id or neesa_lw.DEFAULT_USER
    base = datetime.now(neesa_lw.JST).replace(hour=0, minute=0, second=0, microsecond=0)
    frm = base.isoformat()
    unt = (base + timedelta(days=days)).isoformat()

    events = neesa_lw.get_calendar_events(calendar_id, frm, unt, user_id=user_id)
    out = []
    for e in events:
        start = e.get("start", {}) or {}
        end = e.get("end", {}) or {}
        dt = start.get("dateTime") or start.get("date")
        if not dt:
            continue
        out.append({
            "summary": e.get("summary", ""),
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "all_day": "dateTime" not in start,
        })
    out.sort(key=lambda x: x["start"])
    return out
