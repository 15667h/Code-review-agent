"""租户数据的自助订阅。

租户管理员可以配置「我的报表」，系统每天 8 点把前一天的报表推到
收件人手里。订阅配置以 JSON 保存在租户自己的目录下。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_ROOT
from .export import sanitize_name


@dataclass
class Subscription:
    name: str
    metrics: list[str] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "metrics": list(self.metrics),
            "recipients": list(self.recipients),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Subscription":
        return cls(
            name=str(payload.get("name", "")),
            metrics=[str(item) for item in payload.get("metrics", [])],
            recipients=[str(item) for item in payload.get("recipients", [])],
            enabled=bool(payload.get("enabled", True)),
        )


class SubscriptionStore:
    """一个租户一个文件：``<DATA_ROOT>/<tenant>/subscriptions.json``。"""

    def __init__(self, root: str = DATA_ROOT) -> None:
        self.root = Path(root)

    def _file(self, tenant: str) -> Path:
        return self.root / sanitize_name(tenant) / "subscriptions.json"

    def load(self, tenant: str) -> list[Subscription]:
        path = self._file(tenant)
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [Subscription.from_dict(item) for item in payload.get("items", [])]

    def save(self, tenant: str, items: list[Subscription]) -> None:
        path = self._file(tenant)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"items": [item.to_dict() for item in items]}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def upsert(self, tenant: str, sub: Subscription) -> list[Subscription]:
        items = [item for item in self.load(tenant) if item.name != sub.name]
        items.append(sub)
        self.save(tenant, items)
        return items

    def export_bundle(self, tenant: str) -> str:
        """把订阅打包成一段 JSON 文本，供租户下载备份。"""
        return json.dumps([item.to_dict() for item in self.load(tenant)], ensure_ascii=False, indent=2)

    def remove(self, tenant: str, name: str) -> bool:
        items = self.load(tenant)
        kept = [item for item in items if item.name != name]
        if len(kept) == len(items):
            return False
        self.save(tenant, kept)
        return True


def validate_recipients(addresses: list[str]) -> list[str]:
    """只接受形如 a@b.c 的地址，其余丢弃。"""
    valid: list[str] = []
    for address in addresses:
        local, _, domain = address.partition("@")
        if local and "." in domain and " " not in address:
            valid.append(address)
    return valid


def normalize_recipient(raw: str) -> str:
    """收件人标识：站内信直投用的人名/地址，统一转小写。"""
    return raw.strip().lower()


def parse_metrics(raw: str) -> list[str]:
    """逗号分隔的指标名，去空去重。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def bundle_filename(tenant: str) -> str:
    return f"{sanitize_name(tenant)}-subscriptions.json"
