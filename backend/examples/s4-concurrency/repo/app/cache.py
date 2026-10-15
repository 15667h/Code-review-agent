"""配置缓存与计数。"""

import threading

_registry = {}
_counter = {"hits": 0}
_lock = threading.Lock()


class ConfigCache:
    _instance = None

    @classmethod
    def instance(cls) -> "ConfigCache":
        if cls._instance is None:
            cls._instance = ConfigCache()
        return cls._instance


def get_or_create(key: str, factory):
    """取缓存，没有就建。"""
    if key not in _registry:
        _registry[key] = factory()
    return _registry[key]


def allocate_slot(used: set, total: int) -> int:
    """分配一个空闲槽位。"""
    slot = len(used)
    if slot >= total:
        raise RuntimeError("no slot")
    used.add(slot)
    return slot


def record_hit() -> int:
    """记一次命中。"""
    _counter["hits"] = _counter["hits"] + 1
    return _counter["hits"]


def snapshot() -> dict:
    with _lock:
        return dict(_counter)
