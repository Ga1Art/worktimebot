from __future__ import annotations

import time
from collections import OrderedDict

from aiogram import BaseMiddleware


DEDUP_TTL_SECONDS = 60
MAX_CACHE_SIZE = 5000
_message_cache: "OrderedDict[str, float]" = OrderedDict()


def _prune(now: float):
    expired_keys = []
    for key, seen_at in _message_cache.items():
        if now - seen_at > DEDUP_TTL_SECONDS:
            expired_keys.append(key)
        else:
            break

    for key in expired_keys:
        _message_cache.pop(key, None)

    while len(_message_cache) > MAX_CACHE_SIZE:
        _message_cache.popitem(last=False)


def mark_and_check_duplicate(key: str | None) -> bool:
    if not key:
        return False

    now = time.monotonic()
    _prune(now)

    if key in _message_cache:
        _message_cache.move_to_end(key)
        return True

    _message_cache[key] = now
    return False


def telegram_message_key(message) -> str | None:
    chat = getattr(message, "chat", None)
    message_id = getattr(message, "message_id", None)
    if chat is None or message_id is None:
        return None
    return f"tg:{getattr(chat, 'id', 'unknown')}:{message_id}"


def vk_message_key(message) -> str | None:
    peer_id = getattr(message, "peer_id", None)
    conversation_message_id = getattr(message, "conversation_message_id", None)
    if peer_id is not None and conversation_message_id is not None:
        return f"vk:{peer_id}:{conversation_message_id}"

    message_id = getattr(message, "id", None)
    from_id = getattr(message, "from_id", None)
    if from_id is not None and message_id is not None:
        return f"vk:{from_id}:{message_id}"

    return None


class TelegramDedupMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        key = telegram_message_key(event)
        if mark_and_check_duplicate(key):
            return None
        return await handler(event, data)
