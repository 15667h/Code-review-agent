"""审计日志。

审计行格式：``ts\tactor\taction\tsubject\tresult\tdetail``，只追加，
由日志采集器按行消费。写入失败必须影响调用方 —— 审计不可丢。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import AUDIT_LOG_PATH


class AuditUnavailable(RuntimeError):
    """审计通道不可用。调用方应拒绝继续执行敏感动作。"""


@dataclass
class AuditEvent:
    actor: str
    action: str
    subject: str
    result: str
    detail: str = ""

    def render(self) -> str:
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        return f"{stamp}\t{self.actor}\t{self.action}\t{self.subject}\t{self.result}\t{self.detail}"


class AuditLog:
    """追加写。每条记录独立落盘并 fsync，宁慢勿丢。"""

    def __init__(self, path: str = AUDIT_LOG_PATH) -> None:
        self.path = path

    def write(self, event: AuditEvent) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(event.render() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise AuditUnavailable(str(exc)) from exc

    def recent(self, limit: int = 50) -> list[str]:
        """读最后若干行，供管理面排障。"""
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return []
        return lines[-limit:]

    def write_quietly(self, event: AuditEvent) -> bool:
        """best-effort 版本：采集器知道这条只是提示性记录。"""
        try:
            self.write(event)
            return True
        except AuditUnavailable:
            return False


DEFAULT_AUDIT = AuditLog()
