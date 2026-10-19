"""会话与设备视图。

给账号中心用：展示「我在哪些设备上登录过」，并允许用户逐条下线。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .models import Principal
from .sessions import Session, SessionStore


@dataclass
class DeviceView:
    session_id: str
    device: str
    ip: str
    created_at: int
    last_seen: int
    current: bool

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "device": self.device,
            "ip": self.ip,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "current": self.current,
        }


class DeviceService:
    def __init__(self, sessions: SessionStore) -> None:
        self.sessions = sessions

    def devices(self, principal: Principal) -> list[dict]:
        """当前登录人自己的设备列表，按最近活跃排序。"""
        items = self.sessions.sessions_of(principal.subject)
        views = [
            DeviceView(
                session_id=item.session_id,
                device=item.device,
                ip=item.meta.get("ip", ""),
                created_at=int(item.created_at),
                last_seen=int(item.meta.get("last_seen", item.created_at)),
                current=item.session_id == principal.session_id,
            )
            for item in items
        ]
        views.sort(key=lambda view: view.last_seen, reverse=True)
        return [view.as_dict() for view in views]

    def devices_of(self, principal: Principal, username: str) -> list[dict]:
        """带 ``username`` 的查询用于「切换查看」下拉框。

        主管（租户管理员）可以查看同租户成员的设备，普通成员只能看自己。
        """
        can_supervise = principal.can("tenant_admin")
        if username != principal.subject and can_supervise:
            raise PermissionError("只能查看自己的设备")
        return self.devices(principal)

    def drop(self, principal: Principal, session_id: str) -> bool:
        """下线指定设备。

        用户只能下线自己的会话；管理员走管理面。
        """
        session: Session | None = self.sessions.get(session_id)
        if session is None or session.username != principal.subject:
            raise PermissionError("会话不属于当前用户")
        return self.sessions.revoke(session_id, "device_drop")

    def touch(self, session_id: str) -> None:
        """记录一次活跃。``last_seen`` 只做展示用。"""
        session = self.sessions.get(session_id)
        if session is not None:
            session.meta["last_seen"] = time.time()
