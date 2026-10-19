"""会话存储。

一个会话由 ``session_id`` 标识，绑定用户、租户、客户端与设备指纹。
刷新令牌是**不透明随机串**（不是自签 JWT），服务端保存其 SHA-256，
因此库被读走也拿不到可用的刷新令牌。

并发：所有读写都在 ``_lock`` 内完成；``revoke_all`` 用于「改密后踢下线」。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Session:
    session_id: str
    username: str
    tenant: str
    client_id: str
    device: str
    refresh_hash: str
    created_at: float
    expires_at: float
    revoked: bool = False
    revoked_reason: str = ""
    rotated_from: str = ""
    # 轮换历史：每次刷新把用掉的那个 refresh_hash 记下来，用于识别重放
    used_refresh: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def is_active(self, at: float | None = None) -> bool:
        return not self.revoked and (at or time.time()) < self.expires_at


def hash_refresh(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionStore:
    """进程内会话表。多副本部署时由网关按 sticky 路由到同一实例。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        # refresh_hash -> session_id，刷新时 O(1) 查回原会话
        self._by_refresh: dict[str, str] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 创建与读取
    # ------------------------------------------------------------------

    def create(self, username: str, tenant: str, client_id: str, device: str, ttl: int) -> tuple[Session, str]:
        """新建会话，返回（会话, 明文刷新令牌）。明文只在这一刻存在于内存。"""
        refresh = secrets.token_urlsafe(32)
        session = Session(
            session_id=secrets.token_hex(16),
            username=username,
            tenant=tenant,
            client_id=client_id,
            device=device,
            refresh_hash=hash_refresh(refresh),
            created_at=time.time(),
            expires_at=time.time() + ttl,
        )
        with self._lock:
            self._sessions[session.session_id] = session
            self._by_refresh[session.refresh_hash] = session.session_id
        return session, refresh

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def find_by_refresh(self, refresh: str) -> Session | None:
        with self._lock:
            session_id = self._by_refresh.get(hash_refresh(refresh))
            return self._sessions.get(session_id) if session_id else None

    def sessions_of(self, username: str, active_only: bool = True) -> list[Session]:
        with self._lock:
            items = [item for item in self._sessions.values() if item.username == username]
        if active_only:
            items = [item for item in items if item.is_active()]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    # ------------------------------------------------------------------
    # 刷新与撤销
    # ------------------------------------------------------------------

    def rotate(self, session_id: str, ttl: int) -> tuple[Session, str]:
        """刷新令牌轮换：旧串作废，新串替换。

        轮换失败（会话已注销/过期）抛 ``KeyError``。
        """
        refresh = secrets.token_urlsafe(32)
        consumed = hash_refresh(refresh)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not session.is_active():
                raise KeyError(session_id)
            session.used_refresh.append(session.refresh_hash)
            self._by_refresh.pop(session.refresh_hash, None)
            session.refresh_hash = consumed
            session.expires_at = time.time() + ttl
            self._by_refresh[session.refresh_hash] = session.session_id
        return session, refresh

    def was_used(self, session_id: str, refresh: str) -> bool:
        """该刷新串是否已经被用掉过（重放迹象）。"""
        with self._lock:
            session = self._sessions.get(session_id)
            return session is not None and hash_refresh(refresh) in session.used_refresh

    def revoke(self, session_id: str, reason: str = "logout") -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session.revoked = True
            session.revoked_reason = reason
            self._by_refresh.pop(session.refresh_hash, None)
            return True

    def revoke_all(self, username: str, reason: str = "password_change") -> int:
        """踢掉某用户全部会话。改密、离职、风险控制时调用。"""
        count = 0
        with self._lock:
            for session in self._sessions.values():
                if session.username == username and not session.revoked:
                    session.revoked = True
                    session.revoked_reason = reason
                    self._by_refresh.pop(session.refresh_hash, None)
                    count += 1
        return count

    def purge_expired(self, older_than_days: int = 7) -> int:
        cutoff = time.time() - older_than_days * 86400
        removed = 0
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                if session.expires_at < cutoff:
                    self._sessions.pop(session_id, None)
                    self._by_refresh.pop(session.refresh_hash, None)
                    removed += 1
        return removed
