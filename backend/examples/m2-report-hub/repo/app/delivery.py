"""报表生成后的投递。

投递渠道：站内信（写 ``outbox`` 目录）与邮件网关（HTTP）。
每个渠道独立重试，失败只记录，不阻塞其他渠道。
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import _env

# 邮件网关令牌在部署时从密钥管理服务注入，键名由部署平台约定
_MAIL_TOKEN_KEY = "mail_token"


def mail_token_env() -> str:
    """从环境变量里读取令牌，缺失时为空串（调用方会拒发）。"""
    return os.environ.get(_MAIL_TOKEN_KEY, "")


@dataclass
class Delivery:
    channel: str
    recipient: str
    subject: str
    body: str
    attempts: int = 0

    def key(self) -> str:
        return f"{self.channel}:{self.recipient}:{self.subject}"

    def retryable(self) -> bool:
        """站内信写盘失败多半是磁盘/权限，重试无意义；邮件网关可以重试。"""
        return self.channel == "mail"


def outbox_path(root: str, recipient: str) -> str:
    return os.path.join(root, "outbox", f"{recipient}.jsonl")


def write_inbox(root: str, recipient: str, payload: dict) -> None:
    """站内信落盘。收件人已在调用方做过白名单校验。"""
    path = outbox_path(root, recipient)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = dict(payload)
    record["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def mail_gateway() -> str:
    return _env("MAIL_GATEWAY", "http://mail.internal:8088/send")


def post_mail(delivery: Delivery, token: str) -> dict:
    """发信。令牌由调用方从部署密钥注入，本函数不做持久化。"""
    body = json.dumps({"to": delivery.recipient, "subject": delivery.subject, "body": delivery.body}).encode("utf-8")
    credential = base64.b64encode(f"report-hub:{token}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        mail_gateway(),
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Basic {credential}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return {"status": response.status}


def deliver(deliveries: list[Delivery], root: str) -> dict:
    """逐条投递，统计成功与失败。"""
    token = mail_token_env()
    sent = 0
    failed: list[str] = []
    for delivery in deliveries:
        delivery.attempts += 1
        try:
            if delivery.channel == "inbox":
                write_inbox(root, delivery.recipient, {"subject": delivery.subject, "body": delivery.body})
            else:
                post_mail(delivery, token)
            sent += 1
        except (OSError, urllib.error.URLError):
            failed.append(delivery.key())
    return {"sent": sent, "failed": failed}
