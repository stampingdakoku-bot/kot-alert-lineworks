"""
LINE WORKSカレンダーへの予定作成。既存neesa_lw.pyの読み取り専用サービスアカウント
(JWT)を流用し、書き込み時のみ scope=calendar のトークンを別途取得する
(scope=calendar.readのトークンでは書き込み不可、Phase 0スパイクで検証済み)。
"""
import time
import logging
import requests
import jwt

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


def create_event(summary, start_dt, end_dt, calendar_id=None, user_id=None):
    """予定を作成する。戻り値: (success: bool, detail)"""
    calendar_id = calendar_id or DEFAULT_CALENDAR_ID
    user_id = user_id or neesa_lw.DEFAULT_USER
    try:
        token = _get_write_token()
    except Exception as e:
        logger.error("カレンダー書き込みトークン取得失敗: %s", e)
        return False, str(e)

    url = f"{neesa_lw.API_BASE}/users/{user_id}/calendars/{calendar_id}/events"
    body = {
        "eventComponents": [{
            "summary": summary,
            "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tokyo"},
            "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tokyo"},
        }]
    }
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
