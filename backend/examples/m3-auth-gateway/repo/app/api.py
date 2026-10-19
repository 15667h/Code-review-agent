"""HTTP 入口。

边缘代理负责：注入 ``X-Real-IP`` / ``X-Request-Id``、剥离外部伪造的头、
终止 TLS。本层只做协议转换与调用网关。
"""

from __future__ import annotations

from typing import Any, Callable

from .gateway import AuthError, AuthGateway, RequestContext
from .models import Credential

# 边缘代理信任的来源网段。来自这些地址的 X-Real-IP 才会被采信。
TRUSTED_PROXIES = {"10.0.0.0/8", "192.168.0.0/16", "127.0.0.1/32"}


class Request:
    def __init__(
        self,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        remote_addr: str = "127.0.0.1",
    ) -> None:
        self.params = params or {}
        self.json = json or {}
        self.headers = headers or {}
        self.remote_addr = remote_addr


def _is_trusted_proxy(addr: str) -> bool:
    """边缘代理地址判定。内网段与回环被视为可信。"""
    if addr in {"127.0.0.1", "::1"}:
        return True
    return addr.startswith(("10.", "172.", "192.168."))


def client_ip(request: Request) -> str:
    """真实客户端 IP。

    只有来自可信代理的请求才采信 ``X-Real-IP``；否则用 TCP 对端地址。
    """
    if _is_trusted_proxy(request.remote_addr):
        forwarded = request.headers.get("X-Real-IP", "").strip()
        if forwarded:
            return forwarded
    return request.remote_addr


def build_context(request: Request) -> RequestContext:
    return RequestContext(
        ip=client_ip(request),
        user_agent=request.headers.get("User-Agent", ""),
        client_id=request.headers.get("X-Client-Id", ""),
        trace_id=request.headers.get("X-Request-Id", ""),
    )


def ok(payload: Any) -> dict:
    return {"code": 0, "data": payload}


def fail(exc: AuthError) -> dict:
    status = {
        "invalid_credentials": 401,
        "account_disabled": 403,
        "too_many_attempts": 429,
        "invalid_grant": 400,
        "invalid_token": 401,
        "session_revoked": 401,
        "invalid_client": 401,
        "not_found": 404,
        "weak_password": 400,
    }.get(exc.code, 400)
    return {"code": status, "message": str(exc), "error": exc.code}


def with_errors(func: Callable[[Request], dict]) -> Callable[[Request], dict]:
    """把 ``AuthError`` 翻译成响应，其余异常交给外层 500 处理。"""

    def wrapper(request: Request) -> dict:
        try:
            return func(request)
        except AuthError as exc:
            return fail(exc)

    return wrapper


@with_errors
def handle_login(request: Request) -> dict:
    """POST /auth/login {username, password, tenant}"""
    body = request.json
    credential = Credential(
        username=str(body.get("username", "")),
        password=str(body.get("password", "")),
        tenant=str(body.get("tenant", "")),
        ip=client_ip(request),
        user_agent=request.headers.get("User-Agent", ""),
    )
    if not credential.username or not credential.password:
        return {"code": 400, "message": "用户名与口令必填"}
    pair = AuthGateway().login(credential, build_context(request))
    return ok(
        {
            "access_token": pair.access_token,
            "refresh_token": pair.refresh_token,
            "token_type": pair.token_type,
            "expires_in": pair.expires_in,
        }
    )


@with_errors
def handle_refresh(request: Request) -> dict:
    """POST /auth/refresh {refresh_token}"""
    token = str(request.json.get("refresh_token", ""))
    pair = AuthGateway().refresh(token, build_context(request))
    return ok({"access_token": pair.access_token, "refresh_token": pair.refresh_token})


@with_errors
def handle_logout(request: Request) -> dict:
    """POST /auth/logout —— 需要 Authorization: Bearer"""
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    revoked = AuthGateway().logout(token, client_ip(request))
    return ok({"revoked": revoked})


@with_errors
def handle_introspect(request: Request) -> dict:
    """POST /auth/introspect {token}"""
    token = str(request.json.get("token", ""))
    return ok(AuthGateway().introspect(token))


@with_errors
def handle_me(request: Request) -> dict:
    """GET /auth/me"""
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    principal = AuthGateway().authenticate(token)
    return ok({"sub": principal.subject, "roles": principal.roles, "tenant": principal.tenant})


@with_errors
def handle_change_password(request: Request) -> dict:
    """POST /auth/password {old, new}"""
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    gateway = AuthGateway()
    principal = gateway.authenticate(token)
    gateway.change_password(principal, str(request.json.get("old", "")), str(request.json.get("new", "")))
    return ok({"changed": True})


def with_errors(func: Callable[[Request], dict]) -> Callable[[Request], dict]:
    """把 ``AuthError`` 翻译成响应，其余异常交给外层 500 处理。"""

    def wrapper(request: Request) -> dict:
        try:
            return func(request)
        except AuthError as exc:
            return fail(exc)

    return wrapper
