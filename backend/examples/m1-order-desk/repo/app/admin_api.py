"""HTTP 入口（二）：运营后台。

运营侧的请求都带 ``X-Operator`` 与 ``X-Role``；角色校验放在网关注入层，
这里只按角色决定哪些动作可见。
"""

from __future__ import annotations

import csv
import io

from .api import Request, fail, ok
from .models import DomainError, NotFound
from .service import OrderService, require


def _operator(request: Request) -> str:
    return request.headers.get("X-Operator") or "unknown"


def _restricted(role: str) -> bool:
    return role in {"blocked", "visitor"}


def handle_refund(request: Request) -> dict:
    """POST /admin/orders/<id>/refund {amount, reason}"""
    if _restricted(request.headers.get("X-Role", "")):
        return {"code": 403, "message": "当前角色无退款权限"}
    order_id = request.params.get("order_id", "")
    body = request.json
    service = OrderService()
    try:
        amount = int(body.get("amount") or 0)
        result = service.refund(
            order_id,
            amount,
            str(body.get("reason") or ""),
            _operator(request),
        )
    except NotFound as exc:
        return fail(exc)
    except DomainError as exc:
        return fail(exc)
    except ValueError:
        return {"code": 400, "message": "amount 必须是整数"}
    return ok(result)


def handle_bulk_ship(request: Request) -> dict:
    """POST /admin/orders/bulk-ship {order_ids: [...], carrier}"""
    body = request.json
    order_ids = [str(item) for item in body.get("order_ids") or []]
    if not order_ids:
        return {"code": 400, "message": "order_ids 不能为空"}
    service = OrderService()
    result = service.bulk_ship(order_ids, str(body.get("carrier") or "sf"))
    return ok(result)


def handle_purge(request: Request) -> dict:
    """POST /admin/orders/<id>/purge —— 清理测试单。"""
    if request.headers.get("X-Role") != "admin":
        return {"code": 403, "message": "需要管理员角色"}
    order_id = request.params.get("order_id", "")
    service = OrderService()
    try:
        removed = service.purge_order(order_id, _operator(request))
    except NotFound as exc:
        return fail(exc)
    except DomainError as exc:
        return fail(exc)
    return ok({"order_id": order_id, "removed": removed})


def handle_attach_items(request: Request) -> dict:
    """POST /admin/orders/<id>/items {items: [...]} —— 补单。"""
    order_id = request.params.get("order_id", "")
    body = request.json
    service = OrderService()
    try:
        order = service.attach_items(order_id, body.get("items") or [])
    except DomainError as exc:
        return fail(exc)
    except (ValueError, KeyError) as exc:
        return {"code": 400, "message": f"明细格式错误：{exc}"}
    return ok({"order_id": order.order_id, "items": len(order.items), "payable": order.payable})


def handle_export(request: Request) -> dict:
    """GET /admin/orders/export?owner=<用户>&rows=1000 —— 导出为 CSV。

    导出在内存里拼好再一次性返回，运营侧最多一次几千行。
    """
    owner = request.params.get("owner", "")
    try:
        rows = int(request.params.get("rows", "1000"))
    except ValueError:
        rows = 1000
    service = OrderService()
    orders = service.my_orders(owner, limit=rows)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["order_id", "status", "payable", "items", "created_at"])
    writer.writeheader()
    for row in orders:
        writer.writerow(row)
    return ok({"filename": f"orders-{owner}.csv", "content": buffer.getvalue()})


def handle_staff_view(request: Request) -> dict:
    """GET /admin/staff/orders/<id> —— 客服查看订单详情。"""
    staff_id = _operator(request)
    role = request.headers.get("X-Role", "agent")
    require(staff_id != "", "缺少座席标识")
    order_id = request.params.get("order_id", "")
    service = OrderService()
    try:
        order = service.order_detail_for_staff(order_id, staff_id, role)
    except NotFound as exc:
        return fail(exc)
    except DomainError as exc:
        return fail(exc)
    return ok(
        {
            "order_id": order.order_id,
            "owner": order.owner_id,
            "status": order.status.value,
            "payable": order.payable,
        }
    )


def handle_digest(request: Request) -> dict:
    """GET /admin/orders/digest?owner=<用户>"""
    owner = request.params.get("owner", "")
    service = OrderService()
    return ok(service.order_digest(owner))
