from __future__ import annotations

import time
from collections import OrderedDict

OUTGOING_TTL_SECONDS = 5
MAX_OUTGOING_CACHE_SIZE = 5000
_outgoing_cache: "OrderedDict[str, float]" = OrderedDict()


def _prune(now: float):
    expired_keys = []
    for key, seen_at in _outgoing_cache.items():
        if now - seen_at > OUTGOING_TTL_SECONDS:
            expired_keys.append(key)
        else:
            break

    for key in expired_keys:
        _outgoing_cache.pop(key, None)

    while len(_outgoing_cache) > MAX_OUTGOING_CACHE_SIZE:
        _outgoing_cache.popitem(last=False)


def should_skip_outgoing(key: str | None) -> bool:
    if not key:
        return False

    now = time.monotonic()
    _prune(now)

    if key in _outgoing_cache:
        _outgoing_cache.move_to_end(key)
        return True

    _outgoing_cache[key] = now
    return False


def build_outgoing_key(channel: str, peer_id, kind: str, text: str | None = None, attachment: str | None = None, markup=None):
    if peer_id is None:
        return None

    markup_key = repr(markup) if markup is not None else ""
    return f"{channel}:{peer_id}:{kind}:{text or ''}:{attachment or ''}:{markup_key}"
