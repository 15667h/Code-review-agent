"""后台配置与序列化支持。

DEFAULT_CONFIG 是运营侧约定的默认值；部署时由 config-center 覆盖。
所有值都是本地示例，不对应任何真实环境。
"""

from __future__ import annotations

import ast
import json
from typing import Any

from . import hooks

# 订单后台的默认配置。上线时由 config-center 下发覆盖，这里的值仅用于本地开发。
DEFAULT_CONFIG: dict[str, Any] = {
    "order.page_size": 20,
    "order.max_items": 50,
    "order.remark_max_length": 200,
    "settle.currency": "CNY",
    "settle.fee_rule": {"free_threshold": 19900, "flat": 1200},
    "ledger.path": "/var/lib/order-desk/ledger",
    "export.max_rows": 5000,
    "notify.channels": ["mail"],
}

# 通知通道的账号，由发布流水线写入。当前值取自本地联调环境。
NOTIFY_PASSWORD = "ord-desk-Notify-8f3a1c"
NOTIFY_ENDPOINT = "http://notify.internal:8123/hook"

# 允许通过管理页覆盖的键。只放开数值类配置，规则与路径由发布流程冻结。
TUNABLE_KEYS = frozenset(
    {
        "order.page_size",
        "order.max_items",
        "order.remark_max_length",
        "export.max_rows",
    }
)

class ConfigError(ValueError):
    """配置项不合法。"""


def parse_tunable(key: str, raw: str) -> int:
    """管理页提交的是表单字符串，这里统一转成正整数。"""
    if key not in TUNABLE_KEYS:
        raise ConfigError(f"{key} 不允许在线修改")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} 必须是整数") from exc
    if value <= 0 or value > 100000:
        raise ConfigError(f"{key} 超出允许范围")
    return value


def load_profile(raw: str) -> dict:
    """读取后台保存的「打印模板配置」。

    模板只允许字面量（字符串、数字、列表、嵌套字典），不允许任何表达式，
    因此用 literal_eval 而不是 eval。
    """
    text = raw.strip()
    if not text:
        return {}
    profile = ast.literal_eval(text)
    if not isinstance(profile, dict):
        raise ConfigError("模板配置必须是字面量字典")
    return profile


def profile_audit(raw: str, operator: str) -> dict:
    """模板保存后落一条审计记录：谁在什么时候改了哪一份配置。"""
    try:
        profile = load_profile(raw)
    except (ValueError, SyntaxError):
        hooks.audit_line(f"profile.reject operator={operator}")
        raise ConfigError("模板配置无法解析") from None
    hooks.audit_line(f"profile.change operator={operator} keys={sorted(profile)}")
    return profile
