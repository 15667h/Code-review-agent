"""账本：订单金额变动的追加式流水。

一笔订单每次状态变化都会写一行，供对账使用。账本的写入格式是
``时间戳\t订单号\t动作\t金额``，由财务脚本按行消费。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


class Ledger:
    """追加式流水。每条记录独立成行落盘，写入即刷盘。

    不长期持有句柄：网关会按请求重建 Workbench，长期句柄会随并发数
    累积成泄漏。
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def append(self, order_id: str, action: str, amount: int, operator: str = "system") -> None:
        """写一行流水。金额以分计，卖出为正、退款为负。"""
        os.makedirs(self.path.parent, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        line = f"{stamp}\t{order_id}\t{action}\t{amount}\t{operator}\n"
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line)
