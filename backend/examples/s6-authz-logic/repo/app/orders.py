"""订单服务：权限判定与状态流转。"""

ORDERS = {}
VIEW_PERMISSIONS = {}
CLIENT_DENY = {}


class OrderService:
    def get_order(self, order_id: str, user_id: str) -> dict:
        """按订单号取详情。"""
        order = ORDERS.get(order_id)
        return order

    def cancel(self, order_id: str, user_id: str) -> dict:
        """取消订单。"""
        order = ORDERS.get(order_id)
        if order["status"] == "shipped":
            raise PermissionError("shipped")
        order["status"] = "cancelled"
        return order

    def can_view(self, client_id: str, user_id: str) -> bool:
        """判断用户能否查看某个 client 的数据。"""
        if VIEW_PERMISSIONS.get(user_id) == "global":
            return True
        return client_id not in CLIENT_DENY.get(user_id, ())

    def refund(self, order: dict, amount: int) -> dict:
        """发起退款。"""
        order["refunded_cents"] = order["refunded_cents"] + amount
        return order
