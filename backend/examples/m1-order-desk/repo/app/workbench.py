"""一次下单会话的上下文：仓储、库存、账本与配置。

``service`` 只依赖本模块，生产环境把 ``Store`` 换成 SQL 实现时业务代码不用动。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from .config import DEFAULT_CONFIG
from .inventory import InventoryService
from .ledger import Ledger
from .orm import Store, default_store
from .repository import OrderRepository, StockRepository

if TYPE_CHECKING:
    from .models import Order


class MemoryWorkbench:
    """一个会话绑定一份仓储、库存与账本。"""

    def __init__(self, store: Store | None = None, ledger_path: str | None = None) -> None:
        self.store = store or default_store()
        self.orders = OrderRepository(self.store)
        self.stocks = StockRepository(self.store)
        self.inventory = InventoryService(self.stocks)
        self.ledger = Ledger(ledger_path or DEFAULT_CONFIG["ledger.path"])
        self.config: dict[str, Any] = dict(DEFAULT_CONFIG)
        self._order_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def lock_for(self, order_id: str) -> threading.Lock:
        """取（必要时创建）某订单的专属锁，用于串行化同一订单的写操作。"""
        with self._locks_guard:
            lock = self._order_locks.get(order_id)
            if lock is None:
                lock = threading.Lock()
                self._order_locks[order_id] = lock
            return lock


def create_waybill(order: Order, carrier: str, tracking_no: str) -> str:
    """生成面单号占位：承运商编码 + 下单时间戳 + 订单尾号。

    真实实现会同步调用承运商打单接口，这里是本地替身。
    """
    stamp = order.created_at.strftime("%y%m%d%H%M")
    return f"{carrier.upper()}-{stamp}-{order.order_id[-4:]}"
