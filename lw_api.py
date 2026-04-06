"""
kot-alert: LINE WORKS Bot API クライアント
"""
import jwt
import time
import requests
import logging
from config import (
    LW_CLIENT_ID, LW_CLIENT_SECRET,
    LW_SERVICE_ACCOUNT_ID, LW_BOT_ID,
    LW_PRIVATE_KEY_PATH, LW_DOMAIN_ID
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://auth.worksmobile.com/oauth2/v2.0/token"
API_BASE = "https://www.worksapis.com/v1.0"

_token_cache = {"access_token": None, "expires_at": 0}

def _load_private_key():
    with open(LW_PRIVATE_KEY_PATH, "r") as f:
        return f.read()

def _create_jwt():
    now = int(time.time())
    payload = {
        "iss": LW_CLIENT_ID,
        "sub": LW_SERVICE_ACCOUNT_ID,
        "iat": now,
        "exp": now + 3600,
    }
    private_key = _load_private_key()
    token = jwt.encode(payload, private_key, algorithm="RS256")
    return token

def get_access_token():
    now = int(time.time())
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]
    logger.info("LINE WORKS アクセストークン取得中...")
    assertion = _create_jwt()
    data = {
        "assertion": assertion,
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id": LW_CLIENT_ID,
        "client_secret": LW_CLIENT_SECRET,
        "scope": "bot user.read calendar.read group.read",
    }
    try:
        resp = requests.post(AUTH_URL, data=data, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        _token_cache["access_token"] = result["access_token"]
        _token_cache["expires_at"] = now + int(result.get("expires_in", 3600))
        logger.info("アクセストークン取得成功")
        return result["access_token"]
    except Exception as e:
        logger.error(f"アクセストークン取得失敗: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response: {e.response.text}")
        return None

def send_message(user_id, text):
    token = get_access_token()
    if not token:
        logger.error("アクセストークンがないためメッセージ送信不可")
        return False
    url = f"{API_BASE}/bots/{LW_BOT_ID}/users/{user_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    body = {"content": {"type": "text", "text": text}}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        logger.info(f"メッセージ送信成功: {user_id}")
        return True
    except Exception as e:
        logger.error(f"メッセージ送信失敗 ({user_id}): {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response: {e.response.text}")
        return False

def send_group_message(channel_id, text, silent=False):
    token = get_access_token()
    if not token:
        logger.error("アクセストークンがないためグループメッセージ送信不可")
        return False
    url = f"{API_BASE}/bots/{LW_BOT_ID}/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    body = {"content": {"type": "text", "text": text}}
    if silent:
        body["notificationDisabled"] = True
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        logger.info(f"グループメッセージ送信成功: {channel_id}")
        return True
    except Exception as e:
        logger.error(f"グループメッセージ送信失敗 ({channel_id}): {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response: {e.response.text}")
        return False

def test_connection():
    token = get_access_token()
    if token:
        print(f"[OK] LINE WORKS 認証成功 (token: {token[:20]}...)")
        return True
    else:
        print("[NG] LINE WORKS 認証失敗")
        return False
