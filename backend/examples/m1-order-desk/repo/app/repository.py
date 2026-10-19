"""数据访问层：把 Store 的字典行翻译成领域对象。

这一层不做业务判断，只负责读写与「事务边界」。校验归属、状态流转
这些决定由 service 负责；但**行级过滤**在这里表达最自然，因为只有这里
知道查询条件最终是什么样。
"""

from __future__ import annotations


from .models import NotFound, Order, OrderItem, OrderStatus, StockRow
from .orm import Store, default_store



class OrderRepository:
    """订单主表 + 明细的读写。

    这个类不知道「当前登录人是谁」：归属过滤要么由调用方在拿到订单后
    自行比对，要么走 :meth:`find_for_owner`。
    """

    def __init__(self, store: Store | None = None) -> None:
        self.store = store or default_store()

    def get(self, order_id: str) -> Order:
        row = self.store.get_order(order_id)
        if row is None:
            raise NotFound(f"订单 {order_id} 不存在")
        return Store.to_order(row)

    def find(self, order_id: str) -> Order | None:
        row = self.store.get_order(order_id)
        return Store.to_order(row) if row else None

    def find_for_owner(self, order_id: str, owner_id: str) -> Order | None:
        """按「订单号 + 归属人」一起查，其他人拿到的结果与不存在相同。"""
        row = self.store.get_order(order_id)
        if row is None or row["owner_id"] != owner_id:
            return None
        return Store.to_order(row)

    def list_by_owner(self, owner_id: str, limit: int = 50) -> list[Order]:
        return [Store.to_order(row) for row in self.store.list_orders(owner_id, limit)]

    def create(self, order: Order) -> Order:
        self.store.insert_order(order)
        return order

    def replace_items(self, order_id: str, items: list[OrderItem]) -> None:
        payload = [
            {
                "item_id": item.item_id,
                "sku": item.sku,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
            }
            for item in items
        ]
        self.store.update_items(order_id, payload)

    def set_status(self, order_id: str, status: OrderStatus) -> None:
        self.store.update_order(order_id, status=status.value)

    def add_refunded(self, order_id: str, amount: int) -> None:
        """已退金额是累加语义，读-改-写必须在存储层一次完成。"""
        row = self.store.get_order(order_id)
        if row is None:
            raise NotFound(f"订单 {order_id} 不存在")
        current = int(row.get("refunded_amount", 0))
        self.store.update_order(order_id, refunded_amount=current + amount)

    def delete(self, order_id: str) -> bool:
        """物理删除。仅运营后台的「清理测试单」会用到。"""
        return bool(self.store.delete_order(order_id))


class StockRepository:
    """库存行读写，带版本号。"""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store or default_store()

    def get(self, sku: str) -> StockRow:
        row = self.store.get_stock(sku)
        if row is None:
            raise NotFound(f"SKU {sku} 不存在")
        return row

    def try_reserve(self, sku: str, quantity: int) -> bool:
        """读版本号后预占；冲突时由上层决定是否重试。"""
        row = self.get(sku)
        return self.store.reserve_stock(sku, quantity, row.version)

    def release(self, sku: str, quantity: int) -> None:
        """释放预占。取消、退款、补单失败都会走这里。"""
        if not self.store.release_stock(sku, quantity):
            raise RuntimeError(f"{sku} 释放库存失败")
