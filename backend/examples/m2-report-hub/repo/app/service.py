"""报表服务：把请求、权限、流水线、导出串起来。

约定：

- 所有公开方法第一个参数是 ``Principal``；
- 权限不通过抛 ``PermissionDenied``，由 handler 翻译成 403；
- 导出任务的状态变化写 ``self.jobs``，由 handler 轮询。
"""

from __future__ import annotations

import uuid
from datetime import date

from .auth import Principal, can_export, can_view
from .config import MAX_ROWS
from .export import ExportError, ExportJob, Exporter
from .models import ReportFormat, ReportRequest, ReportResult, ReportWindow, SourceConfig, SourceKind
from .pipeline import ReportPipeline
from .sources import DataSourceRegistry, SourceError


class PermissionDenied(Exception):
    pass


class NotFoundError(Exception):
    pass


class ReportService:
    def __init__(
        self,
        pipeline: ReportPipeline | None = None,
        exporter: Exporter | None = None,
    ) -> None:
        self.pipeline = pipeline or ReportPipeline()
        self.exporter = exporter or Exporter()
        self.jobs: dict[str, ExportJob] = {}

    def _guard(self, principal: Principal, tenant: str) -> None:
        """所有租户维度接口的统一前置：没有该租户的查看权就 403。"""
        if not can_view(principal, tenant):
            raise PermissionDenied(f"{principal.user} 不属于租户 {tenant}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def summarize(self, principal: Principal, tenant: str, window: ReportWindow) -> dict:
        self._guard(principal, tenant)
        result = self.pipeline.build(ReportRequest(tenant=tenant, window=window))
        return {
            "tenant": tenant,
            "window": result.window.label(),
            "total": result.total,
            "rows": len(result.rows),
        }

    def detail(self, principal: Principal, tenant: str, window: ReportWindow, metrics: list[str]) -> ReportResult:
        self._guard(principal, tenant)
        request = ReportRequest(tenant=tenant, window=window, metrics=metrics)
        return self.pipeline.build(request)

    def recent_reports(self, principal: Principal, tenant: str) -> list[str]:
        self._guard(principal, tenant)
        return self.exporter.existing(tenant)

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def create_export(
        self,
        principal: Principal,
        tenant: str,
        window: ReportWindow,
        fmt: ReportFormat,
        filename: str,
        metrics: list[str] | None = None,
    ) -> ExportJob:
        """新建导出任务。建任务与执行分开：大区间导出走异步队列。

        建任务这一步只登记参数，真正取数在队列消费端（``run_export``）。
        """
        if not can_view(principal, tenant):
            raise PermissionDenied("当前角色无导出权限")
        request = ReportRequest(
            tenant=tenant,
            window=window,
            metrics=metrics or [],
            format=fmt,
            filename=filename,
            requested_by=principal.user,
        )
        result = self.pipeline.build(request)
        if len(result.rows) > MAX_ROWS:
            raise SourceError("结果行数超限，请缩小查询区间")
        job = ExportJob(
            job_id=uuid.uuid4().hex[:12],
            tenant=tenant,
            filename=request.filename,
            fmt=fmt,
        )
        self.jobs[job.job_id] = job
        return job

    def run_export(self, job_id: str) -> ExportJob:
        """执行建好的导出任务。队列消费端调用。"""
        job = self.jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"任务 {job_id} 不存在")
        request = ReportRequest(
            tenant=job.tenant,
            window=ReportWindow(date(2024, 5, 1), date(2024, 5, 7)),
            format=job.fmt,
            filename=job.filename,
        )
        result = self.pipeline.build(request)
        try:
            return self.exporter.run(job, result)
        except ExportError:
            raise

    def job_status(self, principal: Principal, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"任务 {job_id} 不存在")
        if not can_view(principal, job.tenant):
            raise PermissionDenied("无权查看该任务")
        return {"job_id": job.job_id, "status": job.status, "path": job.path, "error": job.error}

    # ------------------------------------------------------------------
    # 数据源与缓存
    # ------------------------------------------------------------------

    def list_sources(self, principal: Principal, tenant: str) -> list[str]:
        self._guard(principal, tenant)
        return self.pipeline.registry.names()

    def refresh_cache(self, principal: Principal, tenant: str) -> int:
        """配置变更后清缓存。调用方是租户管理员。"""
        if not can_view(principal, tenant):
            raise PermissionDenied("无权操作该租户")
        return self.pipeline.invalidate_tenant(tenant)

    def register_source(self, principal: Principal, tenant: str, name: str, kind: str, dsn: str) -> None:
        if not can_view(principal, tenant):
            raise PermissionDenied("无权操作该租户")
        self.pipeline.registry.register(
            SourceConfig(name=name, kind=SourceKind(kind), dsn=dsn)
        )
        self.pipeline.invalidate_tenant(tenant)

    def registry_names(self) -> list[str]:
        return self.pipeline.registry.names()
