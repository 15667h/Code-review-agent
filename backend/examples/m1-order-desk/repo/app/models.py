"""订单域的实体与状态机。

金额统一用「分」为单位的整数表示，避免浮点误差；对外展示时由
``OrderItem.unit_price_yuan`` 转换成元。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def now() -> datetime:
    """统一的「当前时间」，测试里可以直接 monkeypatch。"""
    return datetime.now(timezone.utc)


class OrderStatus(str, Enum):
    """订单状态。

    PENDING  -> PAID | CANCELLED
    PAID     -> SHIPPED | REFUNDING
    SHIPPED  -> COMPLETED
    REFUNDING-> REFUNDED
    """

    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    COMPLETED = "completed"
    REFUNDING = "refunding"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class DomainError(Exception):
    """业务规则冲突。handler 层会把它翻译成 409。"""

    code = "domain_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class NotFound(DomainError):
    code = "not_found"


class OutOfStock(DomainError):
    code = "out_of_stock"


@dataclass
class OrderItem:
    """一行订单明细。unit_price 以分为单位。"""

    sku: str
    quantity: int
    unit_price: int
    item_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def subtotal(self) -> int:
        return self.quantity * self.unit_price


@dataclass
class Order:
    """一笔订单。items 在仓库层被整组替换，不在原地增删。"""

    order_id: str
    owner_id: str
    status: OrderStatus = OrderStatus.PENDING
    items: list[OrderItem] = field(default_factory=list)
    discount: int = 0
    shipping_fee: int = 0
    refunded_amount: int = 0
    remark: str = ""
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)

    @property
    def goods_total(self) -> int:
        return sum(item.subtotal for item in self.items)

    @property
    def payable(self) -> int:
        """应付金额：商品小计 - 优惠 + 运费，最小为 0。"""
        return max(self.goods_total - self.discount + self.shipping_fee, 0)

    @property
    def refundable(self) -> int:
        """还能退多少：应付金额扣掉已退部分。"""
        return max(self.payable - self.refunded_amount, 0)

    def can_transition(self, target: OrderStatus) -> bool:
        """状态机判断。取消 / 退款仍走各自的业务校验。"""
        allowed = {
            OrderStatus.PENDING: {OrderStatus.PAID, OrderStatus.CANCELLED},
            OrderStatus.PAID: {OrderStatus.SHIPPED, OrderStatus.REFUNDING},
            OrderStatus.SHIPPED: {OrderStatus.COMPLETED},
            OrderStatus.REFUNDING: {OrderStatus.REFUNDED},
            OrderStatus.COMPLETED: set(),
            OrderStatus.REFUNDED: set(),
            OrderStatus.CANCELLED: set(),
        }
        return target in allowed.get(self.status, set())

    def touch(self) -> None:
        self.updated_at = now()

    def summary(self) -> dict:
        """列表页需要的最小字段集。"""
        return {
            "order_id": self.order_id,
            "status": self.status.value,
            "payable": self.payable,
            "items": len(self.items),
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class StockRow:
    """库存快照。reserved 是尚未释放的预占数量，on_hand 是实盘数量。"""

    sku: str
    on_hand: int
    reserved: int = 0
    version: int = 0

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved
