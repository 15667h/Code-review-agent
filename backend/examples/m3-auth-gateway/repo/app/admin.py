"""管理接口。

网关自身的管理面：查看会话、踢人、重置口令、查询审计。
调用方必须是平台管理员（角色 ``admin``）。
"""

from __future__ import annotations

from .audit import DEFAULT_AUDIT, AuditLog
from .gateway import AuthError, AuthGateway, RequestContext
from .models import DIRECTORY, Principal, UserDirectory


class AdminError(Exception):
    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.status = status


def require_admin(principal: Principal) -> None:
    """只有平台管理员可以进入管理面。"""
    if not principal.can("admin"):
        raise AdminError("需要平台管理员权限", status=403)


def tenant_admin(principal: Principal, tenant: str) -> None:
    """租户管理员只能操作自己租户下的账号。"""
    if principal.can("admin"):
        return
    if principal.can("tenant_admin") and principal.tenant == tenant:
        return
    raise AdminError("无权操作该租户", status=403)


class AdminService:
    def __init__(
        self,
        gateway: AuthGateway | None = None,
        users: UserDirectory | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.gateway = gateway or AuthGateway()
        self.users = users or DIRECTORY
        self.audit = audit or DEFAULT_AUDIT

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    def list_sessions(self, principal: Principal, username: str) -> list[dict]:
        """管理面按用户名查会话，需要平台管理员。"""
        require_admin(principal)
        return [
            {
                "session_id": item.session_id,
                "created_at": item.created_at,
                "expires_at": item.expires_at,
                "device": item.device,
                "client_id": item.client_id,
            }
            for item in self.gateway.sessions.sessions_of(username)
        ]

    def list_user_sessions(self, principal: Principal, username: str) -> list[dict]:
        """账号详情页里「当前登录设备」列表。

        管理面另有 ``list_sessions``，本方法供账号页按用户名直查。
        """
        user = self.users.get(username)
        if user is None:
            raise AdminError("用户不存在", status=404)
        if not principal.can("admin"):
            tenant_admin(principal, user.tenant)
        sessions = self.gateway.sessions.sessions_of(username)
        return [
            {
                "session_id": item.session_id,
                "device": item.device,
                "ip": item.meta.get("ip", ""),
                "created_at": item.created_at,
            }
            for item in sessions
        ]

    def kick(self, principal: Principal, session_id: str) -> bool:
        require_admin(principal)
        return self.gateway.revoke_session(session_id, principal.subject)

    def kick_user(self, principal: Principal, username: str) -> int:
        user = self.users.get(username)
        if user is None:
            raise AdminError("用户不存在", status=404)
        tenant_admin(principal, user.tenant)
        return self.gateway.revoke_user(username, principal.subject)

    # ------------------------------------------------------------------
    # 账号
    # ------------------------------------------------------------------

    def reset_password(self, principal: Principal, username: str, new_password: str) -> str:
        return self.gateway.admin_reset_password(principal, username, new_password)

    def unlock_user(self, principal: Principal, username: str) -> int:
        user = self.users.get(username)
        if user is None:
            raise AdminError("用户不存在", status=404)
        require_admin(principal)
        return self.gateway.unlock(username, principal.subject)

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def tail_audit(self, principal: Principal, limit: int = 50) -> list[str]:
        if not principal.can("admin"):
            raise AdminError("需要平台管理员权限", status=403)
        return self.audit.recent(limit)

    def change_role(self, principal: Principal, username: str, role: str, add: bool = True) -> list[str]:
        """授予/收回角色。租户管理员只能动同等或更低级别的好角色。"""
        user = self.users.get(username)
        if user is None:
            raise AdminError("用户不存在", status=404)
        if principal.can("admin"):
            allowed = True
        else:
            tenant_admin(principal, user.tenant)
            allowed = role in {"viewer", "member"}
        if not allowed:
            raise AdminError("无权授予该角色", status=403)
        if add and role not in user.roles:
            user.roles.append(role)
        if not add and role in user.roles:
            user.roles.remove(role)
        return list(user.roles)
