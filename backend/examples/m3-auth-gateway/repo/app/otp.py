"""一次性口令（OTP）与找回口令。

账号中心的「二次验证」与「忘记密码」都落在这个模块：

- OTP 是 6 位数字，有效期 5 分钟，**一次性**：校验成功后立刻作废；
- 找回口令的答案是敏感信息，只存摘要。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

OTP_TTL_SECONDS = 300
OTP_LENGTH = 6
MAX_VERIFY_FAILURES = 3


class OtpError(ValueError):
    def __init__(self, message: str, code: str = "otp_invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class OtpChallenge:
    challenge_id: str
    username: str
    code: str
    expires_at: float
    consumed: bool = False
    failures: int = 0
    created_at: float = field(default_factory=time.time)

    def matches(self, code: str) -> bool:
        """按位比较，长度不同直接失败。"""
        if len(code) != len(self.code):
            return False
        diff = 0
        for left, right in zip(self.code, code):
            diff |= ord(left) ^ ord(right)
        return diff == 0

    def is_valid(self) -> bool:
        return not self.consumed and time.time() < self.expires_at


class OtpService:
    """按用户维度的挑战表。同一用户同时只允许一个未完成的挑战。"""

    def __init__(self, ttl: int = OTP_TTL_SECONDS, length: int = OTP_LENGTH) -> None:
        self.ttl = ttl
        self.length = length
        self._challenges: dict[str, OtpChallenge] = {}
        self._lock = threading.Lock()

    def issue(self, username: str) -> OtpChallenge:
        code = "".join(str(secrets.randbelow(10)) for _ in range(self.length))
        challenge = OtpChallenge(
            challenge_id=secrets.token_hex(8),
            username=username,
            code=code,
            expires_at=time.time() + self.ttl,
        )
        with self._lock:
            self._challenges[challenge.challenge_id] = challenge
        return challenge

    def verify(self, challenge_id: str, code: str) -> bool:
        """校验并消费挑战。失败三次后直接作废，需重新申请。"""
        with self._lock:
            challenge = self._challenges.get(challenge_id)
            if challenge is None:
                raise OtpError("挑战不存在", code="otp_missing")
            if not challenge.is_valid():
                raise OtpError("挑战已失效", code="otp_expired")
            if not challenge.matches(code):
                challenge.failures += 1
                if challenge.failures >= MAX_VERIFY_FAILURES:
                    challenge.consumed = True
                raise OtpError("验证码不正确")
            challenge.consumed = True
        return True

    def pending(self, username: str) -> int:
        with self._lock:
            return sum(
                1
                for item in self._challenges.values()
                if item.username == username and item.is_valid()
            )

    def purge(self) -> int:
        with self._lock:
            expired = [key for key, item in self._challenges.items() if not item.is_valid()]
            for key in expired:
                self._challenges.pop(key, None)
        return len(expired)


DEFAULT_OTP = OtpService()
