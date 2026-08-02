from __future__ import annotations

from cachetools import TTLCache

_DELIVERY_TTL_SECONDS = 600  # 10 minutes
_seen_deliveries: TTLCache = TTLCache(maxsize=10_000, ttl=_DELIVERY_TTL_SECONDS)


def is_replay(delivery_id: str) -> bool:
    if not delivery_id:
        return False
    if delivery_id in _seen_deliveries:
        return True
    _seen_deliveries[delivery_id] = True
    return False
