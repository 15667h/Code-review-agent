"""查询缓存。

同一份报表在 TTL 内重复请求直接命中。缓存键由 ``ReportRequest.cache_key``
给出，值是不可变的结果快照。

并发模型：每个键带一把锁，未命中时只有第一个请求去底层取数，
其余请求等待，避免「缓存击穿」把压力原样透传到数据源。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import CACHE_TTL_SECONDS


@dataclass
class Entry:
    value: Any
    expires_at: float
    hits: int = 0


@dataclass
class ReportCache:
    """进程内缓存。多实例部署时各实例各自持有。"""

    ttl: int = CACHE_TTL_SECONDS
    _entries: dict[str, Entry] = field(default_factory=dict)
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)
    stats: dict[str, int] = field(default_factory=lambda: {"hit": 0, "miss": 0})

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    @staticmethod
    def key(tenant: str, window: str, scope: str) -> str:
        return f"{tenant}|{window}|{scope}"

    def peek(self, key: str) -> Entry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._entries.pop(key, None)
            return None
        return entry

    def get_or_load(self, key: str, loader: Callable[[], Any]) -> Any:
        entry = self.peek(key)
        if entry is not None:
            entry.hits += 1
            self.stats["hit"] += 1
            return entry.value

        lock = self._lock_for(key)
        with lock:
            entry = self.peek(key)
            if entry is not None:
                entry.hits += 1
                self.stats["hit"] += 1
                return entry.value
            value = loader()
            self.stats["miss"] += 1
            self._entries[key] = Entry(value=value, expires_at=time.monotonic() + self.ttl)
            return value

    def invalidate(self, tenant: str) -> int:
        """租户的配置变更后清掉它的全部缓存。"""
        removed = 0
        for key in list(self._entries):
            if key.startswith(f"{tenant}|"):
                self._entries.pop(key, None)
                removed += 1
        return removed

    def size(self) -> int:
        return len(self._entries)

    def sweep(self) -> int:
        """清过期项。由定时任务每 5 分钟调用一次。"""
        now = time.monotonic()
        expired = [key for key, entry in self._entries.items() if entry.expires_at < now]
        for key in expired:
            self._entries.pop(key, None)
        return len(expired)


DEFAULT_CACHE = ReportCache()
