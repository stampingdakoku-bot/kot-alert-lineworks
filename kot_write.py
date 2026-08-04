"""
KoT打刻書き込みクライアント。給与に直結する本番書き込みのため、
既存の読み取り専用kot_api.pyとは意図的に分離している。
本体/NeeSa両テナントとも、書き込み専用トークン(KOT_WRITE_TOKEN/NEESA_KOT_WRITE_TOKEN、
日別勤怠データのRead+Writeのみ許可)を使う。
"""
import os
import logging
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

BASE = "https://api.kingtime.jp/v1.0"
JST = timezone(timedelta(hours=9))

TOKENS = {
    "main": os.getenv("KOT_WRITE_TOKEN"),
    "neesa": os.getenv("NEESA_KOT_WRITE_TOKEN"),
}


def submit_timerecord(kot_tenant, employee_key, code, when=None):
    """打刻を登録する。code: '1'=出勤 '2'=退勤。
    戻り値: (success: bool, status_code: int|None, body: str)"""
    token = TOKENS.get(kot_tenant)
    if not token:
        return False, None, "書き込みトークンが設定されていません"
    if when is None:
        when = datetime.now(JST)
    when = when.replace(microsecond=0)
    url = f"{BASE}/daily-workings/timerecord/{employee_key}"
    body = {
        "date": when.strftime("%Y-%m-%d"),
        "time": when.isoformat(),
        "code": str(code),
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        ok = resp.status_code in (200, 201)
        if not ok:
            logger.warning("打刻登録失敗 tenant=%s key=%s status=%s body=%s",
                            kot_tenant, employee_key, resp.status_code, resp.text[:500])
        return ok, resp.status_code, resp.text
    except Exception as e:
        logger.error("打刻登録エラー tenant=%s key=%s: %s", kot_tenant, employee_key, e)
        return False, None, str(e)


def fetch_all_records(kot_tenant, date_str):
    """指定日の全社員分の打刻を一括取得。{employeeKey: [timeRecord,...]}"""
    token = TOKENS.get(kot_tenant)
    if not token:
        return {}
    url = f"{BASE}/daily-workings/timerecord/{date_str}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return {dw["employeeKey"]: dw.get("timeRecord", []) for dw in data.get("dailyWorkings", [])}
    except Exception as e:
        logger.error("打刻一括取得失敗 tenant=%s: %s", kot_tenant, e)
        return {}


def get_today_records(kot_tenant, employee_key, date_str=None):
    if date_str is None:
        date_str = datetime.now(JST).strftime("%Y-%m-%d")
    return fetch_all_records(kot_tenant, date_str).get(employee_key, [])
