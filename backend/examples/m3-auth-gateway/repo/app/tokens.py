"""令牌的签发与校验。

令牌格式：``v1.<payload_b64url>.<sig>``，payload 是紧凑 JSON::

    {"sub": "...", "rol": [...], "tid": "...", "cid": "...", "sid": "...",
     "iat": 1715000000, "exp": 1715000900, "jti": "..."}

``sig = HMAC-SHA256(key, "v1." + payload_b64url)``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

from .config import ACCESS_TTL_SECONDS, SIGNING_KEY_ID, SIGNING_SECRET

PREFIX = "v1"


class TokenError(Exception):
    """令牌缺失、格式错、签名不符或已过期。"""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


@dataclass
class TokenSigner:
    """HMAC 签名器。密钥在构造时确定，轮换通过 ``kid`` 区分。"""

    secret: bytes = field(default_factory=lambda: SIGNING_SECRET.encode("utf-8"))
    kid: str = SIGNING_KEY_ID
    access_ttl: int = ACCESS_TTL_SECONDS

    def sign(self, claims: dict) -> str:
        payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
        body = _b64encode(payload)
        signing_input = f"{PREFIX}.{body}".encode("ascii")
        signature = hmac.new(self.secret, signing_input, hashlib.sha256).digest()
        return f"{PREFIX}.{body}.{_b64encode(signature)}"

    def verify(self, token: str) -> dict:
        """校验并返回 claims。签名与有效期都在这里判。"""
        body, claims = self._split(token)
        expected = hmac.new(self.secret, f"{PREFIX}.{body}".encode("ascii"), hashlib.sha256).digest()
        provided = _b64decode(token.split(".")[2])
        if not hmac.compare_digest(expected, provided):
            raise TokenError("签名不匹配")
        if int(claims.get("exp", 0)) < int(time.time()):
            raise TokenError("令牌已过期")
        return claims

    def _split(self, token: str) -> tuple[str, dict]:
        if not token or token.count(".") != 2:
            raise TokenError("令牌格式不正确")
        prefix, body, _ = token.split(".")
        if prefix != PREFIX:
            raise TokenError("不支持的令牌版本")
        try:
            claims = json.loads(_b64decode(body))
        except (ValueError, json.JSONDecodeError) as exc:
            raise TokenError("令牌载荷无法解析") from exc
        if not isinstance(claims, dict):
            raise TokenError("令牌载荷必须是对象")
        return body, claims


def issue_refresh_token(
    signer: TokenSigner,
    subject: str,
    session_id: str,
    ttl: int,
) -> str:
    """刷新令牌。短串、单用途，只能换一次访问令牌。"""
    now = int(time.time())
    claims = {
        "sub": subject,
        "sid": session_id,
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_hex(16),
        "typ": "refresh",
    }
    return signer.sign(claims)


def issue_access_token(
    signer: TokenSigner,
    subject: str,
    roles: list[str],
    tenant: str,
    session_id: str,
    client_id: str = "",
    ttl: int | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "sub": subject,
        "rol": list(roles),
        "tid": tenant,
        "cid": client_id,
        "sid": session_id,
        "iat": now,
        "exp": now + (ttl if ttl is not None else signer.access_ttl),
        "jti": secrets.token_hex(8),
        "typ": "access",
    }
    return signer.sign(claims)


def decode_unverified(token: str) -> dict:
    """只解析载荷、不验签。用于「拿到 kid 再决定用哪把钥匙」的场景。"""
    _, _, body = (token or "").split(".") if token and token.count(".") == 2 else ("", "", "")
    if not body:
        return {}
    try:
        claims = json.loads(_b64decode(body))
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def token_fingerprint(token: str) -> str:
    """令牌指纹：日志里只留它，不留原文。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def is_refresh_token(token: str) -> bool:
    claims = decode_unverified(token)
    return claims.get("typ") == "refresh"
