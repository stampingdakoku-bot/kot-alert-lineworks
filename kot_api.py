"""
kot-alert: KING OF TIME WebAPI クライアント
"""
import requests
import logging
from datetime import datetime, time
from config import KOT_TOKEN, KOT_BASE_URL

logger = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bearer {KOT_TOKEN}",
    "Content-Type": "application/json"
}

BLOCKED_PERIODS = [
    (time(8, 30), time(10, 0)),
    (time(17, 30), time(18, 30)),
]

def is_api_blocked():
    now = datetime.now().time()
    for start, end in BLOCKED_PERIODS:
        if start <= now <= end:
            return True
    return False

def _get(endpoint, params=None):
    url = f"{KOT_BASE_URL}{endpoint}"
    logger.debug(f"GET {url} params={params}")
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        logger.debug(f"Response: {resp.status_code}")
        return data
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP Error: {e} - {resp.text}")
        return None
    except Exception as e:
        logger.error(f"Request Error: {e}")
        return None

def get_employees(division=None):
    params = {"additionalFields": "emailAddresses"}
    if division:
        params["division"] = division
    return _get("/employees", params)

def get_daily_workings(date_str):
    return _get(f"/daily-workings/{date_str}")

def get_timerecords(date_str, employee_keys=None):
    params = {}
    if employee_keys:
        params["employeeKeys"] = ",".join(employee_keys)
    return _get(f"/daily-workings/timerecord/{date_str}", params)

def get_schedules(date_str):
    return _get(f"/daily-schedules/{date_str}")

def get_overtime_requests(year, month):
    return _get(f"/overtime-requests/{year}/{month}")

def get_timerecord_requests(year, month):
    return _get(f"/timerecord-requests/{year}/{month}")

def parse_timerecords_for_employee(daily_data):
    result = {}
    if not daily_data or "dailyWorkings" not in daily_data:
        return result
    for emp_data in daily_data["dailyWorkings"]:
        key = emp_data.get("employeeKey", "")
        records = emp_data.get("timeRecord", [])
        if isinstance(records, dict):
            records = [records]
        clock_in = None
        clock_out = None
        for rec in records:
            code = str(rec.get("code", ""))
            time_str = rec.get("time", "")
            if time_str:
                try:
                    t = datetime.fromisoformat(time_str)
                except ValueError:
                    continue
                if code == "1" and (clock_in is None or t < clock_in):
                    clock_in = t
                elif code == "2" and (clock_out is None or t > clock_out):
                    clock_out = t
        result[key] = {"clock_in": clock_in, "clock_out": clock_out, "records": records}
    return result

def parse_schedules_for_employee(schedule_data):
    result = {}
    if not schedule_data:
        return result
    items = schedule_data.get("dailySchedules", [])
    for item in items:
        key = item.get("employeeKey", "")
        if not key:
            continue
        start = None
        end = None
        if item.get("clockInSchedule"):
            try:
                start = datetime.fromisoformat(item["clockInSchedule"])
            except (ValueError, TypeError):
                pass
        if item.get("clockOutSchedule"):
            try:
                end = datetime.fromisoformat(item["clockOutSchedule"])
            except (ValueError, TypeError):
                pass
        result[key] = {"start": start, "end": end, "raw": item}
    return result

def has_pending_request(employee_key, year, month):
    ot_data = get_overtime_requests(year, month)
    if ot_data:
        requests_list = ot_data.get("requests", ot_data.get("overtimeRequests", []))
        if isinstance(requests_list, list):
            for req in requests_list:
                if req.get("employeeKey") == employee_key:
                    return True
    tr_data = get_timerecord_requests(year, month)
    if tr_data:
        requests_list = tr_data.get("requests", tr_data.get("timerecordRequests", []))
        if isinstance(requests_list, list):
            for req in requests_list:
                if req.get("employeeKey") == employee_key:
                    return True
    return False
