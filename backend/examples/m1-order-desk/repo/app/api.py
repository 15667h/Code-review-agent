"""HTTP 入口（一）：前台订单页面。

请求对象是 WSGI 适配层注入的薄封装：``request.params`` 是查询串，
``request.json`` 是已解析的请求体，``request.headers`` 是网关注入的头。
"""

from __future__ import annotations

from typing import Any

from .config import DEFAULT_CONFIG
from .models import DomainError, NotFound, OutOfStock
from .service import OrderService


class Request:
    """最小请求抽象，便于在没有 Web 框架时直接构造。"""

    def __init__(
        self,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        user_id: str = "anonymous",
    ) -> None:
        self.params = params or {}
        self.json = json or {}
        self.headers = headers or {}
        self.user_id = user_id


def ok(payload: Any) -> dict:
    return {"code": 0, "data": payload}


def fail(exc: DomainError) -> dict:
    mapping = {
        "not_found": 404,
        "out_of_stock": 409,
        "bad_state": 409,
        "over_refund": 409,
        "domain_error": 400,
    }
    return {"code": mapping.get(exc.code, 400), "message": str(exc)}


def handle_list_orders(request: Request) -> dict:
    """GET /orders?limit=20 —— 只返回当前登录人的订单。"""
    params = request.params
    raw_limit = params.get("limit", "20")
    max_limit = DEFAULT_CONFIG["order.page_size"]
    try:
        limit = int(raw_limit)
    except ValueError:
        return {"code": 400, "message": "limit 必须是整数"}
    limit = min(limit, max_limit)

    service = OrderService()
    try:
        rows = service.my_orders(request.user_id, limit)
    except DomainError as exc:
        return fail(exc)
    return ok(rows)


def handle_get_order(request: Request) -> dict:
    """GET /orders/<id> —— 详情页。"""
    order_id = request.params.get("order_id", "")
    service = OrderService()
    try:
        order = service.get_order(order_id)
    except NotFound as exc:
        return fail(exc)
    return ok(
        {
            "order_id": order.order_id,
            "status": order.status.value,
            "payable": order.payable,
            "refundable": order.refundable,
            "items": [
                {"sku": item.sku, "quantity": item.quantity, "unit_price": item.unit_price}
                for item in order.items
            ],
        }
    )


def handle_create_order(request: Request) -> dict:
    """POST /orders {items: [{sku, quantity, unit_price}], coupon, remark}"""
    body = request.json
    if not isinstance(body.get("items"), list):
        return {"code": 400, "message": "items 必须是数组"}
    service = OrderService()
    try:
        order = service.place_order(
            owner_id=request.user_id,
            items=body.get("items") or [],
            coupon=int(body.get("coupon") or 0),
            remark=str(body.get("remark") or ""),
        )
    except OutOfStock as exc:
        return fail(exc)
    except DomainError as exc:
        return fail(exc)
    except (ValueError, KeyError) as exc:
        return {"code": 400, "message": f"请求体格式错误：{exc}"}
    return ok({"order_id": order.order_id, "payable": order.payable})


def handle_pay(request: Request) -> dict:
    """POST /orders/<id>/pay {amount, channel}"""
    order_id = request.params.get("order_id", "")
    body = request.json
    service = OrderService()
    try:
        service.pay(order_id, int(body.get("amount") or 0), str(body.get("channel") or "wallet"))
    except NotFound as exc:
        return fail(exc)
    except DomainError as exc:
        return fail(exc)
    return ok({"order_id": order_id, "status": "paid"})


def handle_cancel(request: Request) -> dict:
    """POST /orders/<id>/cancel"""
    order_id = request.params.get("order_id", "")
    service = OrderService()
    try:
        result = service.cancel_order(order_id)
    except NotFound as exc:
        return fail(exc)
    except DomainError as exc:
        return fail(exc)
    return ok(result)
