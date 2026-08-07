from __future__ import annotations
import os, hmac
OWNER=str(os.getenv('TELEGRAM_OWNER_CHAT_ID',''))

def is_owner(user_id, chat_id=None):
    if not OWNER or not hmac.compare_digest(str(user_id), OWNER):
        return False
    return chat_id is None or hmac.compare_digest(str(chat_id), OWNER)
