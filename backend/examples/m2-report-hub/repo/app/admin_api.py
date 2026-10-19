"""HTTP 入口：租户管理员后台。

管理员可以维护订阅、下载订阅备份、查看本租户的配额。
所有入口都先加载 ``Principal`` 再做租户级判断。
"""

from __future__ import annotations

from .api import Request, error, ok
from .auth import can_manage_sources, load_principal
from .export import sanitize_name
from .service import PermissionDenied, ReportService
from .subscriptions import (
    Subscription,
    SubscriptionStore,
    bundle_filename,
    normalize_recipient,
    parse_metrics,
    validate_recipients,
)


def _admin_guard(request: Request, tenant: str) -> tuple[str, dict | None]:
    """租户管理员判断。

    平台运营靠 ``X-Tenant-Admin`` 头声明代管关系，代管期间同等对待；
    普通租户成员看角色表。
    """
    principal = load_principal(request.user)
    delegated = request.headers.get("X-Tenant-Admin") == tenant
    role = principal.role_in(tenant)
    if not delegated:
        if role != "admin":
            return "", error(403, f"{principal.user} 不是 {tenant} 的管理员")
    return principal.user, None


def handle_list_subs(request: Request) -> dict:
    """GET /admin/subs?tenant=acme"""
    tenant = request.params.get("tenant", "")
    _, denied = _admin_guard(request, tenant)
    if denied:
        return denied
    store = SubscriptionStore()
    return ok([item.to_dict() for item in store.load(tenant)])


def handle_upsert_sub(request: Request) -> dict:
    """POST /admin/subs {tenant, name, metrics: "gmv,orders", recipients: [...]}"""
    body = request.json
    tenant = str(body.get("tenant", ""))
    _, denied = _admin_guard(request, tenant)
    if denied:
        return denied
    metrics = parse_metrics(str(body.get("metrics", "")))
    if not metrics:
        return error(400, "至少选一个指标")
    recipients = validate_recipients([str(item) for item in body.get("recipients") or []])
    sub = Subscription(name=sanitize_name(str(body.get("name", ""))), metrics=metrics, recipients=recipients)
    try:
        store = SubscriptionStore()
        items = store.upsert(tenant, sub)
    except OSError as exc:
        return error(500, f"保存订阅失败：{exc}")
    return ok({"count": len(items), "recipients": recipients})


def handle_download_bundle(request: Request) -> dict:
    """GET /admin/subs/bundle?tenant=acme —— 把订阅导出成 JSON 文件返回。"""
    tenant = request.params.get("tenant", "")
    store = SubscriptionStore()
    return ok({"filename": bundle_filename(tenant), "content": store.export_bundle(tenant)})


def handle_push_now(request: Request) -> dict:
    """POST /admin/subs/push {tenant, name} —— 立即给订阅人推一份日报。

    紧急补发时用，投递目标是订阅里登记的收件人，需要按同一个规则取。
    """
    body = request.json
    tenant = str(body.get("tenant", ""))
    _, denied = _admin_guard(request, tenant)
    if denied:
        return denied
    store = SubscriptionStore()
    sub = next((item for item in store.load(tenant) if item.name == str(body.get("name", ""))), None)
    if sub is None:
        return error(404, "订阅不存在")
    from .delivery import Delivery, deliver
    from .config import DATA_ROOT

    deliveries = [
        Delivery(channel="inbox", recipient=normalize_recipient(who), subject=f"{tenant} 补发", body="")
        for who in sub.recipients
    ]
    return ok(deliver(deliveries, DATA_ROOT))


def handle_purge_cache(request: Request) -> dict:
    """POST /admin/cache/purge {tenant}"""
    tenant = str(request.json.get("tenant", ""))
    _, denied = _admin_guard(request, tenant)
    if denied:
        return denied
    try:
        removed = ReportService().refresh_cache(load_principal(request.user), tenant)
    except PermissionDenied as exc:
        return error(403, str(exc))
    return ok({"tenant": tenant, "cleared": removed})
