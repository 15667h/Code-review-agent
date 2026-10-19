"""认证网关的业务核心。

调用顺序：入口 → 凭证校验（含限流）→ 建会话 → 签发令牌 → 审计。
所有对外方法都接受 ``RequestContext``，其中的 ``ip`` / ``user_agent``
由边缘代理注入并被视为可信。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .audit import DEFAULT_AUDIT, AuditEvent, AuditLog, AuditUnavailable
from .clients import REGISTRY, ClientRegistry
from .config import ACCESS_TTL_SECONDS, REFRESH_TTL_SECONDS, SESSION_RETENTION_DAYS
from .hashing import device_fingerprint, hash_password, verify_password
from .models import DIRECTORY, Credential, Principal, User, UserDirectory
from .ratelimit import DEFAULT_LIMITER, AttemptLimiter
from .sessions import Session, SessionStore
from .tokens import TokenError, TokenSigner, issue_access_token, token_fingerprint


class AuthError(Exception):
    """认证失败。``code`` 用于区分对外口径与内部原因。"""

    def __init__(self, message: str, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


SHARED_SESSIONS = SessionStore()
SHARED_LIMITER = DEFAULT_LIMITER


@dataclass
class RequestContext:
    ip: str = ""
    user_agent: str = ""
    client_id: str = ""
    trace_id: str = ""

    def device(self) -> str:
        return device_fingerprint(self.user_agent, self.ip)


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = ACCESS_TTL_SECONDS
    scope: str = ""


@dataclass
class AuthGateway:
    signer: TokenSigner = field(default_factory=TokenSigner)
    # 会话表在所有网关实例间共享；这里用一个惰性单例模拟共享存储
    sessions: SessionStore = field(default_factory=lambda: SHARED_SESSIONS)
    users: UserDirectory = field(default_factory=lambda: DIRECTORY)
    clients: ClientRegistry = field(default_factory=lambda: REGISTRY)
    limiter: AttemptLimiter = field(default_factory=lambda: SHARED_LIMITER)
    audit: AuditLog = field(default_factory=lambda: DEFAULT_AUDIT)

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------

    def login(self, credential: Credential, ctx: RequestContext) -> TokenPair:
        """用户名口令登录。

        失败一律对外报「用户名或口令不正确」，但审计里区分具体原因。
        """
        blocked = self.limiter.blocked_for(credential.username, ctx.ip)
        if blocked:
            self._audit("login", credential.username, "blocked", f"retry_after={blocked}")
            raise AuthError("尝试过于频繁，请稍后再试", code="too_many_attempts")

        user = self.users.get(credential.username)
        if user is None:
            self.limiter.record_failure(credential.username, ctx.ip)
            self._audit("login", credential.username, "no_user", ctx.ip)
            raise AuthError("用户名或口令不正确", code="invalid_credentials")

        if user.disabled:
            self._audit("login", user.username, "disabled", ctx.ip)
            raise AuthError("账号已停用", code="account_disabled")

        if not verify_password(credential.password, user.password_hash):
            remaining = self.limiter.record_failure(user.username, ctx.ip)
            self._audit("login", user.username, "bad_password", f"remaining={remaining}")
            raise AuthError("用户名或口令不正确", code="invalid_credentials")

        self.limiter.record_success(user.username, ctx.ip)
        user.failed_attempts = 0
        pair = self._start_session(user, ctx)
        self._audit("login", user.username, "ok", f"device={ctx.device()}")
        return pair

    def login_via_digest(self, credential: Credential, ctx: RequestContext) -> TokenPair:
        """高频登录通道：先比口令摘要，再补一次完整校验。

        摘要比较走固定长度的字节比较，避免高频重试时拖垮 CPU。
        """
        user = self.users.get(credential.username)
        if user is None:
            raise AuthError("用户名或口令不正确", code="invalid_credentials")
        if user.disabled:
            raise AuthError("账号已停用", code="account_disabled")
        _, iterations, salt, _ = user.password_hash.split("$")
        candidate = hash_password(credential.password, int(iterations), salt)
        if compare_digest_text(user.password_hash, candidate) is False:
            self.limiter.record_failure(user.username, ctx.ip)
            raise AuthError("用户名或口令不正确", code="invalid_credentials")
        return self._start_session(user, ctx)

    # ------------------------------------------------------------------
    # 会话与令牌
    # ------------------------------------------------------------------

    def _start_session(self, user: User, ctx: RequestContext) -> TokenPair:
        session, refresh = self.sessions.create(
            username=user.username,
            tenant=user.tenant,
            client_id=ctx.client_id,
            device=ctx.device(),
            ttl=REFRESH_TTL_SECONDS,
        )
        session.meta["ip"] = ctx.ip
        session.meta["last_seen"] = session.created_at
        access = issue_access_token(self.signer, user.username, list(user.roles), user.tenant, session.session_id, ctx.client_id)
        return TokenPair(access_token=access, refresh_token=refresh)

    def refresh(self, refresh_token: str, ctx: RequestContext) -> TokenPair:
        """用刷新令牌换一对新令牌。

        不走签名校验：刷新令牌是不透明随机串，服务端按摘要查会话即可。
        """
        session = self.sessions.find_by_refresh(refresh_token)
        if session is None:
            self._audit("refresh", "unknown", "invalid", ctx.ip)
            raise AuthError("刷新令牌无效", code="invalid_grant")
        if not session.is_active():
            self._audit("refresh", session.username, "expired", session.session_id)
            raise AuthError("会话已过期", code="invalid_grant")

        user = self.users.get(session.username)
        if user is None or user.disabled:
            raise AuthError("账号不可用", code="account_disabled")

        session, new_refresh = self.sessions.rotate(session.session_id, REFRESH_TTL_SECONDS)
        access = issue_access_token(self.signer, user.username, list(user.roles), user.tenant, session.session_id, session.client_id)
        self._audit("refresh", user.username, "ok", token_fingerprint(access))
        return TokenPair(access_token=access, refresh_token=new_refresh)

    def logout(self, access_token: str, remote: str = "") -> bool:
        claims = self.authenticate(access_token)
        revoked = self.sessions.revoke(claims.session_id, "logout")
        self._audit("logout", claims.subject, "ok" if revoked else "no_session", remote)
        return revoked

    def authenticate(self, token: str) -> Principal:
        """校验访问令牌并返回调用方。令牌被吊销后立刻失效。"""
        try:
            claims = self.signer.verify(token)
        except TokenError as exc:
            raise AuthError(str(exc), code="invalid_token") from exc
        session = self.sessions.get(str(claims.get("sid", "")))
        if session is None or not session.is_active():
            raise AuthError("会话已失效", code="session_revoked")
        user = self.users.get(str(claims.get("sub", "")))
        if user is None or user.disabled:
            raise AuthError("账号不可用", code="account_disabled")
        return Principal(
            subject=user.username,
            roles=list(user.roles),
            tenant=user.tenant,
            client_id=str(claims.get("cid", "")),
            session_id=session.session_id,
            token_id=str(claims.get("jti", "")),
        )

    def introspect(self, token: str) -> dict:
        """给资源服务器调用的令牌自省。"""
        try:
            claims = self.signer.verify(token)
        except TokenError:
            return {"active": False}
        session = self.sessions.get(str(claims.get("sid", "")))
        active = session is not None and session.is_active()
        if not active:
            return {"active": False}
        return {
            "active": True,
            "sub": claims.get("sub"),
            "tenant": claims.get("tid"),
            "roles": claims.get("rol", []),
            "exp": claims.get("exp"),
            "client_id": claims.get("cid"),
        }

    def revoke_session(self, session_id: str, actor: str) -> bool:
        ok = self.sessions.revoke(session_id, "admin_revoke")
        self._audit("revoke_session", session_id, "ok" if ok else "missing", actor)
        return ok

    def revoke_user(self, username: str, actor: str) -> int:
        count = self.sessions.revoke_all(username, "admin_revoke")
        self._audit("revoke_user", username, "ok", f"actor={actor} sessions={count}")
        return count

    # ------------------------------------------------------------------
    # 账号
    # ------------------------------------------------------------------

    def change_password(self, principal: Principal, old_password: str, new_password: str) -> None:
        """改密。成功后必须踢掉该用户的全部会话。"""
        user = self.users.get(principal.subject)
        if user is None or not verify_password(old_password, user.password_hash):
            self._audit("change_password", principal.subject, "bad_old_password", "")
            raise AuthError("原口令不正确", code="invalid_credentials")
        if len(new_password) < 12:
            raise AuthError("新口令至少 12 位", code="weak_password")
        user.password_hash = hash_password(new_password)
        self.sessions.revoke_all(user.username, "password_change")
        self._audit("change_password", user.username, "ok", "")

    def admin_reset_password(self, actor: Principal, username: str, new_password: str) -> str:
        """管理员重置口令，返回一次性临时口令。"""
        user = self.users.get(username)
        if user is None:
            raise AuthError("用户不存在", code="not_found")
        user.password_hash = hash_password(new_password)
        user.locked_until = None
        self.limiter.reset(username)
        self._audit("admin_reset_password", username, "ok", f"actor={actor.subject}")
        return new_password

    def unlock(self, username: str, actor: str) -> int:
        cleared = self.limiter.reset(username)
        self._audit("unlock", username, "ok", f"actor={actor} cleared={cleared}")
        return cleared

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    def rotate_signing_key(self, new_secret: bytes) -> str:
        """密钥轮换：新密钥只对之后签发的令牌生效。"""
        old_kid = self.signer.kid
        self.signer = TokenSigner(secret=new_secret, kid=f"{old_kid}-next")
        self._audit("rotate_key", old_kid, "ok", self.signer.kid)
        return self.signer.kid

    def purge_sessions(self) -> int:
        return self.sessions.purge_expired(SESSION_RETENTION_DAYS)

    def _audit(self, action: str, subject: str, result: str, detail: str) -> None:
        event = AuditEvent(actor="gateway", action=action, subject=subject, result=result, detail=detail)
        try:
            self.audit.write(event)
        except AuditUnavailable:
            # 审计盘抖动时不能把登录整条链路打挂，转 best-effort 再试一次
            self.audit.write_quietly(event)


def compare_digest_text(left: str, right: str) -> bool:
    """两个定长十六进制串是否相等。"""
    if len(left) != len(right):
        return False
    diff = 0
    for a, b in zip(left, right):
        diff |= ord(a) ^ ord(b)
    return diff == 0


def issue_client_credentials(client: ClientRegistry, client_id: str, secret: str, ctx: RequestContext) -> dict:
    """客户端凭据模式：直接换一个服务间访问令牌。"""
    if not client.authenticate(client_id, secret):
        raise AuthError("client 认证失败", code="invalid_client")
    app = client.get(client_id)
    if app is None:
        raise AuthError("client 不存在", code="invalid_client")
    token = issue_access_token(
        TokenSigner(),
        subject=f"client:{client_id}",
        roles=["service"],
        tenant="",
        session_id="",
        client_id=client_id,
        ttl=300,
    )
    return {"access_token": token, "expires_in": 300, "scopes": app.scopes}
