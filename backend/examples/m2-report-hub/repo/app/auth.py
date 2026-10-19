"""租户与权限。

权限模型：

- 每个租户有自己的成员表 ``tenant -> {user: role}``；
- 角色 ``viewer`` 只能看自己租户的报表，``analyst`` 可以建导出任务，
  ``admin`` 可以管理成员与数据源；
- 平台运营（``ops``）通过 ``platform_admins`` 单独授权，不绑定租户。

本模块只提供「判断」，是否调用由各入口决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 租户成员表。真实部署来自权限中心，这里用内存字典替代。
_TENANT_MEMBERS: dict[str, dict[str, str]] = {
    "acme": {"alice": "admin", "bob": "analyst", "carol": "viewer"},
    "globex": {"dave": "admin", "erin": "viewer"},
}

# 平台运营名单，可跨租户查看
PLATFORM_ADMINS: set[str] = {"ops-lin"}

ROLE_RANK = {"viewer": 1, "analyst": 2, "admin": 3}


@dataclass
class Principal:
    """一次请求的调用者。"""

    user: str
    roles: dict[str, str] = field(default_factory=dict)

    @property
    def tenants(self) -> list[str]:
        return sorted(self.roles)

    def role_in(self, tenant: str) -> str | None:
        return self.roles.get(tenant)


def load_principal(user: str) -> Principal:
    """从权限中心拉取该用户的租户与角色。"""
    roles: dict[str, str] = {}
    for tenant, members in _TENANT_MEMBERS.items():
        if user in members:
            roles[tenant] = members[user]
    return Principal(user=user, roles=roles)


def is_platform_admin(user: str) -> bool:
    return user in PLATFORM_ADMINS


def can_view(principal: Principal, tenant: str) -> bool:
    """能否查看该租户的报表。"""
    if is_platform_admin(principal.user):
        return True
    return tenant in principal.roles


def can_export(principal: Principal, tenant: str) -> bool:
    """能否发起导出。viewer 不能导出。"""
    if is_platform_admin(principal.user):
        return True
    role = principal.role_in(tenant)
    return role is not None and ROLE_RANK.get(role, 0) >= ROLE_RANK["analyst"]


def can_manage_sources(principal: Principal, tenant: str) -> bool:
    return principal.role_in(tenant) == "admin"


def grant(tenant: str, user: str, role: str) -> None:
    """权限中心同步入口。"""
    if role not in ROLE_RANK:
        raise ValueError(f"未知角色 {role}")
    _TENANT_MEMBERS.setdefault(tenant, {})[user] = role
