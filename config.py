import os
from dotenv import load_dotenv

load_dotenv()

KOT_TOKEN = os.getenv("KOT_TOKEN")
KOT_BASE_URL = "https://api.kingtime.jp/v1.0"

LW_CLIENT_ID = os.getenv("LW_CLIENT_ID")
LW_CLIENT_SECRET = os.getenv("LW_CLIENT_SECRET")
LW_SERVICE_ACCOUNT_ID = os.getenv("LW_SERVICE_ACCOUNT_ID", "vwm4y.serviceaccount@avivastarscorporation")
LW_BOT_ID = os.getenv("LW_BOT_ID", "11845418")
LW_PRIVATE_KEY_PATH = os.getenv("LW_PRIVATE_KEY_PATH", "/home/ubuntu/kot-alert-lineworks/private_key.pem")
LW_DOMAIN_ID = os.getenv("LW_DOMAIN_ID", "400183322")

OVERTIME_THRESHOLD_MINUTES = 1
MISSING_PUNCH_MINUTES = 15
MANAGER_CHANNEL_ID = ""

DB_PATH = "/home/ubuntu/kot-alert-lineworks/alert.db"
LOG_PATH = "/home/ubuntu/kot-alert-lineworks/logs/alert.log"
