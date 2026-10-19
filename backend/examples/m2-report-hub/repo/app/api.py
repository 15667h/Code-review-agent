"""HTTP 入口：租户侧的报表接口。

上游网关把登录态解析成 ``X-User`` 头注入，本层据此加载 ``Principal``。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .auth import load_principal
from .models import ReportFormat, ReportWindow
from .service import NotFoundError, PermissionDenied, ReportService


class Request:
    def __init__(
        self,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: str = "",
    ) -> None:
        self.params = params or {}
        self.json = json or {}
        self.headers = headers or {}
        self.body = body

    @property
    def user(self) -> str:
        return self.headers.get("X-User", "")


def ok(payload: Any) -> dict:
    return {"code": 0, "data": payload}


def error(code: int, message: str) -> dict:
    return {"code": code, "message": message}


def parse_window(params: dict[str, str], default_days: int = 7) -> ReportWindow:
    """``start`` / ``end`` 必须成对出现，缺省为最近 7 天。"""
    start_raw = params.get("start")
    end_raw = params.get("end")
    if not start_raw or not end_raw:
        today = date(2024, 5, 7)
        return ReportWindow(date.fromordinal(today.toordinal() - default_days + 1), today)
    return ReportWindow(date.fromisoformat(start_raw), date.fromisoformat(end_raw))


def parse_metrics(raw: str) -> list[str]:
    """``gmv,orders`` -> 去空、去重、保持顺序。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _translated(func) -> Any:
    """统一把服务层异常翻译成响应。"""

    def wrapper(request: Request) -> dict:
        principal = load_principal(request.user)
        if not principal.user:
            return error(401, "未登录")
        try:
            return func(request, principal)
        except PermissionDenied as exc:
            return error(403, str(exc))
        except NotFoundError as exc:
            return error(404, str(exc))
        except ValueError as exc:
            return error(400, str(exc))

    return wrapper


@_translated
def handle_summary(request: Request, principal) -> dict:
    """GET /report/summary?tenant=acme&start=..&end=.."""
    tenant = request.params.get("tenant", "")
    window = parse_window(request.params)
    service = ReportService()
    return ok(service.summarize(principal, tenant, window))


@_translated
def handle_detail(request: Request, principal) -> dict:
    """GET /report/detail?tenant=acme&start=..&end=..&metrics=gmv,orders"""
    tenant = request.params.get("tenant", "")
    window = parse_window(request.params)
    metrics = parse_metrics(request.params.get("metrics", ""))
    service = ReportService()
    result = service.detail(principal, tenant, window, metrics)
    return ok(
        {
            "tenant": result.tenant,
            "window": result.window.label(),
            "rows": result.rows,
            "warnings": result.warnings,
        }
    )


@_translated
def handle_export(request: Request, principal) -> dict:
    """POST /report/export {tenant, start, end, format, filename, metrics}"""
    body = request.json
    tenant = str(body.get("tenant", ""))
    window = ReportWindow(date.fromisoformat(body["start"]), date.fromisoformat(body["end"]))
    fmt = ReportFormat(body.get("format", "csv"))
    filename = str(body.get("filename") or f"{tenant}-report")
    metrics = [str(item) for item in body.get("metrics") or []]
    service = ReportService()
    job = service.create_export(principal, tenant, window, fmt, filename, metrics)
    return ok({"job_id": job.job_id, "status": job.status})


@_translated
def handle_job(request: Request, principal) -> dict:
    """GET /report/job?id=..."""
    service = ReportService()
    return ok(service.job_status(principal, request.params.get("id", "")))


@_translated
def handle_recent(request: Request, principal) -> dict:
    """GET /report/recent?tenant=acme"""
    service = ReportService()
    return ok(service.recent_reports(principal, request.params.get("tenant", "")))
