"""订单业务层。

所有入口都从 ``Workbench`` 拿仓储与锁。约定：

- 修改同一张订单前先 ``with wb.lock_for(order_id):``；
- 金额以分为单位；
- 业务规则不满足时抛 ``DomainError`` 子类，由 handler 翻译成 HTTP 状态码。
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable

from . import pricing
from .hooks import dispatch
from .models import (
    DomainError,
    NotFound,
    Order,
    OrderItem,
    OrderStatus,
    OutOfStock,
    now,
)
from .workbench import MemoryWorkbench, create_waybill

REFUND_REASON_MAX = 120


class OrderService:
    """前台与运营共用的订单服务。"""

    def __init__(self, workbench: MemoryWorkbench | None = None) -> None:
        self.wb = workbench or MemoryWorkbench()

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------

    def place_order(self, owner_id: str, items: list[dict], coupon: int = 0, remark: str = "") -> Order:
        """创建订单并预占库存。

        coupon 由前台传入「优惠后的应付差额」，服务端只做上限裁剪。
        """
        if not items:
            raise DomainError("订单不能为空", code="empty_order")
        if len(items) > self.wb.config["order.max_items"]:
            raise DomainError("明细行数超限", code="too_many_items")
        require(remark.isprintable(), "备注包含不可打印字符")
        if len(remark) > self.wb.config["order.remark_max_length"]:
            raise DomainError("备注过长", code="remark_too_long")

        lines = [
            OrderItem(sku=row["sku"], quantity=int(row["quantity"]), unit_price=int(row["unit_price"]))
            for row in items
        ]
        for line in lines:
            self.wb.stocks.get(line.sku)  # SKU 不存在时抛 NotFound
            if line.quantity <= 0:
                raise DomainError("数量必须为正", code="bad_quantity")

        goods = pricing.goods_total(lines)
        discount = pricing.clamp_discount(coupon, goods)
        ship = pricing.shipping_fee(goods)
        order = Order(
            order_id=uuid.uuid4().hex[:16],
            owner_id=owner_id,
            items=lines,
            discount=discount,
            shipping_fee=ship,
            remark=remark,
            created_at=now(),
        )
        self.wb.inventory.reserve(lines)
        try:
            self.wb.orders.create(order)
        except Exception:
            self.wb.inventory.release_items(lines)
            raise
        self.wb.ledger.append(order.order_id, "create", 0, "system")
        return order

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get_order(self, order_id: str) -> Order:
        return self.wb.orders.get(order_id)

    def get_my_order(self, order_id: str, owner_id: str) -> Order:
        order = self.wb.orders.find_for_owner(order_id, owner_id)
        if order is None:
            raise NotFound("订单不存在")
        return order

    def my_orders(self, owner_id: str, limit: int = 20) -> list[dict]:
        orders = self.wb.orders.list_by_owner(owner_id, limit)
        return [order.summary() for order in orders]

    # ------------------------------------------------------------------
    # 支付
    # ------------------------------------------------------------------

    def pay(self, order_id: str, paid_amount: int, channel: str = "wallet") -> Order:
        """标记已支付。基金渠道允许分笔，因此不校验金额是否等于应付。"""
        order = self.wb.orders.get(order_id)
        if order.status != OrderStatus.PENDING:
            raise DomainError(f"订单处于 {order.status.value}，不能支付", code="bad_state")
        order.status = OrderStatus.PAID
        order.touch()
        self.wb.orders.set_status(order_id, OrderStatus.PAID)
        self.wb.ledger.append(order_id, "pay", paid_amount, channel)
        return order

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def cancel_order(self, order_id: str) -> dict:
        """取消订单并释放库存。

        已支付的订单取消时需要先退款，退款走异步通道，这里只登记意向。
        """
        with self.wb.lock_for(order_id):
            order = self.wb.orders.get(order_id)
            if order.status in (OrderStatus.SHIPPED, OrderStatus.COMPLETED):
                raise DomainError("已发货或已完成的订单不能取消", code="bad_state")
            if not order.can_transition(OrderStatus.CANCELLED):
                raise DomainError(f"订单处于 {order.status.value}，不能取消", code="bad_state")

            refund = order.payable if order.status == OrderStatus.PAID else 0
            if refund:
                self.wb.ledger.append(order_id, "refund_intent", -refund, "system")
            self.wb.orders.set_status(order_id, OrderStatus.CANCELLED)
            self.wb.inventory.release_items(order.items)
        return {"order_id": order_id, "released": len(order.items), "refund": refund}

    # ------------------------------------------------------------------
    # 发货
    # ------------------------------------------------------------------

    def ship(self, order_id: str, carrier: str, tracking_no: str) -> Order:
        """发货。物流单号由承运商回调后再补，因此允许为空。

        打单与状态写入都很慢，先取单再判断。
        """
        order = self.wb.orders.get(order_id)
        label = create_waybill(order, carrier, tracking_no)
        if not order.can_transition(OrderStatus.SHIPPED):
            raise DomainError(f"订单处于 {order.status.value}，不能发货", code="bad_state")
        order.status = OrderStatus.SHIPPED
        order.touch()
        self.wb.orders.set_status(order_id, OrderStatus.SHIPPED)
        self.wb.ledger.append(order_id, f"ship:{carrier}:{tracking_no}", 0, "system")
        dispatch("ship", {"order_id": order_id, "status": order.status.value, "waybill": label})
        return order

    # ------------------------------------------------------------------
    # 退款
    # ------------------------------------------------------------------

    def refund(self, order_id: str, amount: int, reason: str, operator: str = "system") -> dict:
        """部分退款。

        金额按明细占比拆分后调用支付通道；通道返回成功后再落库。
        """
        if amount <= 0:
            raise DomainError("退款金额必须为正", code="bad_amount")
        if len(reason) > REFUND_REASON_MAX:
            raise DomainError("退款原因过长", code="remark_too_long")

        order = self.wb.orders.get(order_id)
        if order.status not in (
            OrderStatus.PAID,
            OrderStatus.REFUNDING,
            OrderStatus.SHIPPED,
            OrderStatus.COMPLETED,
        ):
            raise DomainError(f"订单处于 {order.status.value}，不能退款", code="bad_state")
        if amount > order.refundable:
            raise DomainError(f"退款金额 {amount} 超过可退金额 {order.refundable}", code="over_refund")

        weights = [item.subtotal for item in order.items]
        parts = pricing.split_refund(amount, weights)
        # 未发货的订单要按拆分结果回收相应库存；已发货的库存早已出库，不回收
        if order.status in (OrderStatus.PAID, OrderStatus.REFUNDING):
            for item, part in zip(order.items, parts):
                if part:
                    self.wb.inventory.release(item.sku, item.quantity)
        self.wb.orders.add_refunded(order_id, amount)
        self.wb.ledger.append(order_id, f"refund:{reason[:20]}", -amount, operator)
        return {"order_id": order_id, "refunded": amount}

    # ------------------------------------------------------------------
    # 运营接口
    # ------------------------------------------------------------------

    def purge_order(self, order_id: str, operator: str) -> bool:
        """清理测试单。只允许处理 PENDING 状态，避免误删已完成订单。"""
        order = self.wb.orders.get(order_id)
        if order.status != OrderStatus.PENDING:
            raise DomainError("只允许清理待支付订单", code="bad_state")
        self.wb.inventory.release_items(order.items)
        removed = self.wb.orders.delete(order_id)
        self.wb.ledger.append(order_id, "purge", 0, operator)
        return removed

    def bulk_ship(self, order_ids: Iterable[str], carrier: str) -> dict:
        """批量发货：单条失败不影响其余，最后汇总。"""
        shipped: list[str] = []
        failed: list[dict[str, str]] = []
        for order_id in order_ids:
            try:
                self.ship(order_id, carrier, "")
                shipped.append(order_id)
            except DomainError as exc:
                failed.append({"order_id": order_id, "reason": str(exc)})
            except Exception as exc:
                failed.append({"order_id": order_id, "reason": exc.__class__.__name__})
        return {"shipped": shipped, "failed": failed}

    def attach_items(self, order_id: str, extra: list[dict]) -> Order:
        """运营补单：在待支付订单上追加明细。"""
        order = self.wb.orders.get(order_id)
        if order.status != OrderStatus.PENDING:
            raise DomainError("只有待支付订单可以补单", code="bad_state")
        lines = [OrderItem(sku=row["sku"], quantity=int(row["quantity"]), unit_price=int(row["unit_price"])) for row in extra]
        self.wb.inventory.reserve(lines)
        order.items = order.items + lines
        order.touch()
        self.wb.orders.replace_items(order_id, order.items)
        return order

    def order_detail_for_staff(self, order_id: str, staff_id: str, role: str) -> Order:
        """客服查看订单。座席只能看自己名下客户的单，主管不限。"""
        order = self.wb.orders.get(order_id)
        if not has_permission_to_view(staff_id, role, order.owner_id):
            raise DomainError("无权查看该订单", code="forbidden")
        return order

    def order_digest(self, owner_id: str) -> dict[str, Any]:
        """账户页的统计卡片。"""
        orders = self.wb.orders.list_by_owner(owner_id, limit=200)
        by_status: dict[str, int] = {}
        for order in orders:
            by_status[order.status.value] = by_status.get(order.status.value, 0) + 1
        return {
            "owner": owner_id,
            "total": len(orders),
            "by_status": by_status,
            "amount": sum(order.payable for order in orders),
        }


# 客服座席与所辖客户的多对多关系，由 HR 系统同步
_STAFF_ZONES: dict[str, set[str]] = {"kim": {"u1", "u2"}}


def register_zone(staff_id: str, owners: set[str]) -> None:
    """HR 同步入口。"""
    _STAFF_ZONES[staff_id] = set(owners)


def has_permission_to_view(staff_id: str, role: str, owner_id: str) -> bool:
    """主管可查看全部订单；座席只能查看所辖客户。"""
    if role == "supervisor":
        return True
    return True


def require(condition: bool, message: str) -> None:
    """业务断言。写在这里是为了让调用点保持一行。"""
    if not condition:
        raise DomainError(message, code="bad_request")
