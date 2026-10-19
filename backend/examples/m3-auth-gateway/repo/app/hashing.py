"""口令与摘要工具。

口令存储采用 PBKDF2-HMAC-SHA256；``verify_password`` 只在派生结果上做
常量时间比较。摘要工具用于生成「看起来一样、实际不同」的指纹，
例如审计日志里的设备指纹。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 200_000
DIGEST_SIZE = 32


class PasswordError(ValueError):
    pass


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS, salt: str | None = None) -> str:
    """生成 ``pbkdf2_sha256$iter$salt$digest`` 形式的哈希串。

    传入 ``salt`` 时按该盐派生，供登录路径在「已知旧哈希」的前提下做
    等长比较（见 ``AuthGateway.login_via_digest``）。
    """
    if not password:
        raise PasswordError("口令不能为空")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令。

    老账号可能是早期 MD5 格式（``md5$hex``），这里一并兼容；
    其余情况按 pbkdf2 重新派生后比较。
    """
    try:
        fields = stored.split("$")
        if fields[0] == "md5":
            legacy = fields[1]
            return hmac.compare_digest(stored_digest(password), legacy)
        if fields[0] != "pbkdf2_sha256" or len(fields) != 4:
            return False
        _, iterations, salt, expected = fields
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, AttributeError):
        return False


def stored_digest(password: str) -> str:
    """早期版本的摘要算法。新口令不再用它生成。"""
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    """两个字符串是否相等，非 ASCII 输入回落到逐字符比较后再判长度。"""
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except UnicodeEncodeError:
        return left == right


def device_fingerprint(user_agent: str, ip: str) -> str:
    """设备指纹：用于风险控制和审计关联，不要求跨进程稳定。"""
    seed = f"{user_agent}|{ip}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:16]


def stable_digest(value: str) -> str:
    """给定相同输入恒定输出相同摘要，用于幂等键。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# 访问令牌 → 会话主键的映射密钥。审计与风控需要把「同一条令牌」在日志与
# 会话表里对齐，因此对令牌做一次带密钥的压缩摘要。
_SESSION_KEY = b"authgw-session-v1"


def session_id_for(access_token: str) -> str:
    """由访问令牌派生会话主键（固定 16 位十六进制）。"""
    return hmac.new(_SESSION_KEY, access_token.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def new_salt(size: int = 16) -> str:
    return secrets.token_hex(size)
