import json
import os
import time
from typing import Any, Dict, List, Optional

import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0").strip()

# 24 hours by default
CHAT_TTL_SECONDS = int(os.getenv("CHAT_TTL_SECONDS", "86400"))

# Keep last N messages per conversation
MAX_HISTORY = int(os.getenv("CHAT_MAX_HISTORY", "30"))

_client: Optional[redis.Redis] = None


def _r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _client


def _k_messages(user_id: int, session_key: str) -> str:
    # Unique per login session + account
    return f"chat:{user_id}:{session_key}:messages"


def _k_state(user_id: int, session_key: str) -> str:
    # Pending clarification state etc.
    return f"chat:{user_id}:{session_key}:state"


def append_message(user_id: int, session_key: str, role: str, content: str) -> None:
    role = (role or "").strip().lower()
    if role not in {"user", "assistant", "system"}:
        role = "user"

    msg = {"role": role, "content": content, "ts": int(time.time())}
    key = _k_messages(user_id, session_key)
    r = _r()

    r.rpush(key, json.dumps(msg, ensure_ascii=False))
    r.ltrim(key, -MAX_HISTORY, -1)
    r.expire(key, CHAT_TTL_SECONDS)


def get_history(user_id: int, session_key: str) -> List[Dict[str, Any]]:
    key = _k_messages(user_id, session_key)
    raw = _r().lrange(key, 0, -1) or []
    out: List[Dict[str, Any]] = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except Exception:
            continue
    return out


def set_state(user_id: int, session_key: str, state: Dict[str, Any]) -> None:
    key = _k_state(user_id, session_key)
    r = _r()
    r.set(key, json.dumps(state or {}, ensure_ascii=False))
    r.expire(key, CHAT_TTL_SECONDS)


def get_state(user_id: int, session_key: str) -> Optional[Dict[str, Any]]:
    key = _k_state(user_id, session_key)
    val = _r().get(key)
    if not val:
        return None
    try:
        return json.loads(val)
    except Exception:
        return None


def clear_state(user_id: int, session_key: str) -> None:
    _r().delete(_k_state(user_id, session_key))
