"""用户与角色。

身份来源是上游 HR 系统的只读快照（这里是内存字典）。角色是扁平的字符串，
``admin`` 只用于平台自身的管理接口，租户内的管理靠 ``tenant_admin``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class User:
    """一个可登录的账号。

    ``password_hash`` 的格式为 ``pbkdf2_sha256$<iterations>$<salt>$<digest>``。
    """

    username: str
    display_name: str
    password_hash: str = ""
    roles: list[str] = field(default_factory=list)
    tenant: str = ""
    department: str = ""
    disabled: bool = False
    locked_until: datetime | None = None
    failed_attempts: int = 0
    created_at: datetime = field(default_factory=now_utc)

    def is_locked(self, at: datetime | None = None) -> bool:
        if self.locked_until is None:
            return False
        return (at or now_utc()) < self.locked_until

    def can(self, role: str) -> bool:
        return role in self.roles


@dataclass
class Credential:
    """登录时客户端提交的凭据。"""

    username: str
    password: str
    tenant: str = ""
    device: str = ""
    ip: str = ""
    user_agent: str = ""


@dataclass
class ClientApp:
    """一个接入的第三方应用，用 client_id/secret 换令牌。"""

    client_id: str
    name: str
    redirect_uris: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    confidential: bool = True
    secret_hash: str = ""


@dataclass
class Principal:
    """通过认证之后的调用方。"""

    subject: str
    roles: list[str] = field(default_factory=list)
    tenant: str = ""
    client_id: str = ""
    session_id: str = ""
    token_id: str = ""
    issued_at: datetime = field(default_factory=now_utc)

    def can(self, role: str) -> bool:
        return role in self.roles


class UserDirectory:
    """用户快照。真实部署由同步任务每 15 分钟刷新一次。"""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}

    def put(self, user: User) -> None:
        self._users[user.username] = user

    def get(self, username: str) -> User | None:
        return self._users.get(username)

    def names(self) -> list[str]:
        return sorted(self._users)

    def by_tenant(self, tenant: str) -> list[User]:
        return [user for user in self._users.values() if user.tenant == tenant]


DIRECTORY = UserDirectory()
