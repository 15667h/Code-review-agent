"""伪 ORM：把几张内存表包成带缓存的行级仓储。

真实的部署里 ``Store`` 背后是 psycopg 连接，这里用进程内的字典等价替代，
接口保持一致：取行、改行、写回。
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .models import Order, OrderItem, OrderStatus, StockRow


class Store:
    """两张表的进程内实现：orders 与 stock。

    orders 表按 order_id 主键，items 以行内列表形态挂在行上；stock 表
    按 sku 主键，带乐观锁版本号。所有写操作都在 self._lock 里完成。
    """

    def __init__(self, capacity: int = 50_000) -> None:
        # 容量上限：单进程最多承载的订单行数，超过后由归档任务迁走
        self.capacity = capacity
        self._orders: dict[str, dict[str, Any]] = {}
        self._stock: dict[str, StockRow] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 订单
    # ------------------------------------------------------------------

    def insert_order(self, order: Order) -> None:
        with self._lock:
            self._orders[order.order_id] = self._to_row(order)

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._orders.get(order_id)
            return dict(row) if row else None

    def list_orders(self, owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(row) for row in self._orders.values() if row["owner_id"] == owner_id]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return rows[:limit]

    def update_order(self, order_id: str, **fields: Any) -> int:
        """按字段写回。并发写不检测冲突 —— 调用方负责加锁。"""
        with self._lock:
            row = self._orders.get(order_id)
            if row is None:
                return 0
            row.update(fields)
            return 1

    def update_items(self, order_id: str, items: list[dict[str, Any]]) -> int:
        with self._lock:
            row = self._orders.get(order_id)
            if row is None:
                return 0
            row["items"] = items
            row["updated_at"] = datetime.now(timezone.utc)
            return 1

    def delete_order(self, order_id: str) -> int:
        with self._lock:
            return 1 if self._orders.pop(order_id, None) is not None else 0

    # ------------------------------------------------------------------
    # 库存
    # ------------------------------------------------------------------

    def seed_stock(self, sku: str, on_hand: int) -> None:
        with self._lock:
            self._stock[sku] = StockRow(sku=sku, on_hand=on_hand)

    def get_stock(self, sku: str) -> StockRow | None:
        with self._lock:
            row = self._stock.get(sku)
            return StockRow(**asdict(row)) if row else None

    def release_stock(self, sku: str, quantity: int) -> bool:
        """归还预占：在存储锁内读改写，版本号自增。"""
        with self._lock:
            row = self._stock.get(sku)
            if row is None:
                return False
            row.reserved = max(row.reserved - quantity, 0)
            row.version += 1
            return True

    def reserve_stock(self, sku: str, quantity: int, expected_version: int) -> bool:
        """乐观锁预占：版本不符就返回 False，由调用方重试。"""
        with self._lock:
            row = self._stock.get(sku)
            if row is None or row.version != expected_version:
                return False
            if row.available < quantity:
                return False
            row.reserved += quantity
            row.version += 1
            return True

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    @staticmethod
    def _to_row(order: Order) -> dict[str, Any]:
        return {
            "order_id": order.order_id,
            "owner_id": order.owner_id,
            "status": order.status.value,
            "items": [
                {
                    "item_id": item.item_id,
                    "sku": item.sku,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                }
                for item in order.items
            ],
            "discount": order.discount,
            "shipping_fee": order.shipping_fee,
            "refunded_amount": order.refunded_amount,
            "remark": order.remark,
            "created_at": order.created_at,
            "updated_at": order.updated_at,
        }


_DEFAULT_STORE: Store | None = None
_STORE_LOCK = threading.Lock()


def default_store() -> Store:
    """进程级单例。服务入口会在启动时往里灌商品与库存。"""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _STORE_LOCK:
            if _DEFAULT_STORE is None:
                _DEFAULT_STORE = Store()
    return _DEFAULT_STORE
