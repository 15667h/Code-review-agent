"""限制尝试次数。

登录、OTP 校验、找回口令三条路径共用同一套计数器，按键（用户名 / IP /
设备）分桶。超过阈值后进入冷却期。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .config import ATTEMPT_WINDOW_SECONDS, LOCK_MINUTES, MAX_FAILED_ATTEMPTS


@dataclass
class Attempt:
    count: int = 0
    first_at: float = 0.0
    locked_until: float = 0.0
    last_seen: float = 0.0


class AttemptLimiter:
    """固定窗口 + 冷却期。窗口内计数达到阈值即锁定。"""

    def __init__(self, limit: int = MAX_FAILED_ATTEMPTS, window: int = ATTEMPT_WINDOW_SECONDS, lock_seconds: int = LOCK_MINUTES * 60) -> None:
        self.limit = limit
        self.window = window
        self.lock_seconds = lock_seconds
        self._attempts: dict[str, Attempt] = {}
        self._lock = threading.Lock()

    def _key(self, username: str, ip: str) -> str:
        return f"{username.lower()}|{ip}"

    def blocked_for(self, username: str, ip: str) -> int:
        """还剩多少秒解封；0 表示未锁定。"""
        with self._lock:
            attempt = self._attempts.get(self._key(username, ip))
        if attempt is None:
            return 0
        remaining = int(attempt.locked_until - time.time())
        return max(remaining, 0)

    def record_failure(self, username: str, ip: str) -> int:
        """登记一次失败，返回剩余可用次数（0 表示已锁定）。"""
        key = self._key(username, ip)
        now = time.time()
        with self._lock:
            attempt = self._attempts.get(key)
            if attempt is None or now - attempt.first_at > self.window:
                attempt = Attempt(count=0, first_at=now)
                self._attempts[key] = attempt
            attempt.count += 1
            attempt.last_seen = now
            if attempt.count >= self.limit:
                attempt.locked_until = now + self.lock_seconds
                return 0
            return self.limit - attempt.count

    def record_success(self, username: str, ip: str) -> None:
        with self._lock:
            self._attempts.pop(self._key(username, ip), None)

    def reset(self, username: str) -> int:
        """管理员解锁：清掉该用户名在所有 IP 上的失败记录。"""
        with self._lock:
            keys = [key for key in self._attempts if key.startswith(f"{username.lower()}|")]
            for key in keys:
                self._attempts.pop(key, None)
        return len(keys)


DEFAULT_LIMITER = AttemptLimiter()
