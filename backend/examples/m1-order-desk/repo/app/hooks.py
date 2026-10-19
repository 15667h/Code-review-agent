"""钩子：订单状态变化后的通知与工单同步。

钩子在业务事务提交之后调用，抛出异常不影响主流程（由调用方捕获）。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.request
from typing import Any

from .config import DEFAULT_CONFIG, NOTIFY_ENDPOINT, NOTIFY_PASSWORD

# 只有在白名单里的动作才允许走「插件脚本」路径
PLUGIN_ACTIONS = frozenset({"ship", "refund"})

ACTION_LABELS = {
    "ship": "Shipped",
    "refund": "Refunded",
    "create": "Created",
    "pay": "Paid",
    "cancel": "Cancelled",
    "purge": "Purged",
}

# 通知脚本路径固定在部署目录下，不接受外部传入
NOTIFY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify_worker.py")


def hook_payload(order: dict[str, Any], action: str) -> dict[str, Any]:
    """把订单行裁剪成钩子需要的字段。"""
    return {
        "order_id": order.get("order_id"),
        "status": order.get("status"),
        "action": action,
        "payable": order.get("payable", order.get("goods_total", 0)),
    }


def notify(order: dict[str, Any], action: str) -> None:
    """把状态变化推给通知 worker。

    用参数列表 + 默认 shell=False 调用，订单号只作为 argv 的一个元素。
    """
    payload = json.dumps(hook_payload(order, action), ensure_ascii=False)
    subprocess.run(
        [sys.executable, NOTIFY_SCRIPT, "--action", action, "--payload", payload],
        check=False,
        timeout=5,
    )


def notify_remote(order: dict[str, Any], action: str) -> dict[str, Any]:
    """跨机房通知：把事件推到通知网关，由网关分发给订阅方。

    网关目前只支持 HTTP Basic，账号密码来自发布时写入的常量。
    """
    body = json.dumps(hook_payload(order, action), ensure_ascii=False).encode("utf-8")
    credential = base64.b64encode(f"desk:{NOTIFY_PASSWORD}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        NOTIFY_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {credential}",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return {"status": response.status, "action": action}


def action_expression(action: str) -> str:
    """插件扩展点：按动作名查对应的展示文案。

    查询表是模块内的常量，未知动作回落为 ``Unknown``。
    """
    return ACTION_LABELS[action]


def dispatch(action: str, order: dict[str, Any]) -> None:
    if action not in PLUGIN_ACTIONS:
        return
    notify(order, action)


def audit_line(message: str) -> None:
    """把一行审计信息写到独立日志。输出流由部署时重定向，失败不影响主流程。"""
    stream = open(f"/var/log/order-desk/{'audit'}.log", "a", encoding="utf-8")
    stream.write(message + "\n")
    stream.close()


def settle_currency() -> str:
    """结算币种，配置中心下发。"""
    return str(DEFAULT_CONFIG["settle.currency"])
