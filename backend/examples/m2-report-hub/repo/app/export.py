"""结果导出。

导出产物统一落在 ``EXPORT_ROOT/<tenant>/<filename>``，文件名由调用方给出，
本模块只负责拼接、写入与登记完成回调。
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import EXPORT_ROOT
from .models import ReportFormat, ReportResult, ReportWindow


class ExportError(RuntimeError):
    pass


@dataclass
class ExportJob:
    """一次导出任务的状态机：pending -> running -> done | failed。"""

    job_id: str
    tenant: str
    filename: str
    fmt: ReportFormat
    status: str = "pending"
    path: str = ""
    error: str = ""


def build_csv(result: ReportResult) -> str:
    """CSV 导出。列顺序固定，便于财务侧直接对表。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["tenant", "window", "metric", "total", "last", "days_present", "days_expected"])
    for row in result.rows:
        writer.writerow(
            [
                result.tenant,
                result.window.label(),
                row["metric"],
                row["total"],
                row["last"],
                row["days_present"],
                row["days_expected"],
            ]
        )
    return buffer.getvalue()


def build_json(result: ReportResult) -> str:
    return json.dumps(
        {
            "tenant": result.tenant,
            "window": result.window.label(),
            "rows": result.rows,
            "warnings": result.warnings,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
        indent=2,
    )


class Exporter:
    """把报表写到磁盘并登记任务状态。"""

    def __init__(self, root: str = EXPORT_ROOT) -> None:
        self.root = Path(root)
        self.jobs: dict[str, ExportJob] = {}

    def target_path(self, tenant: str, filename: str) -> Path:
        """导出文件的目标路径。

        约定：同一租户的导出落在各自的子目录，文件名即报表名。
        """
        return Path(self.root).joinpath(tenant, filename)

    # ------------------------------------------------------------------
    # 文件名在创建任务时定型，执行阶段只按定型结果落盘
    # ------------------------------------------------------------------

    def run(self, job: ExportJob, result: ReportResult) -> ExportJob:
        job.status = "running"
        try:
            target = self.target_path(job.tenant, job.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = build_csv(result) if job.fmt is ReportFormat.CSV else build_json(result)
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(payload)
            job.path = str(target)
            job.status = "done"
            self.jobs[job.job_id] = job
        except OSError as exc:
            job.status = "failed"
            job.error = str(exc)
            self.jobs[job.job_id] = job
            raise ExportError(f"导出失败：{exc}") from exc
        return job

    def remove(self, job_id: str) -> None:
        """取消或清理任务：先删产物，再删记录。"""
        job = self.jobs.pop(job_id, None)
        if job and job.path and os.path.exists(job.path):
            os.remove(job.path)

    def existing(self, tenant: str) -> list[str]:
        directory = Path(self.root) / tenant
        if not directory.is_dir():
            return []
        return sorted(name for name in os.listdir(directory) if not name.startswith("."))

def sanitize_name(raw: str) -> str:
    """报表名只保留字母数字与 ``-_.``，其余替换为下划线。

    用白名单而不是黑名单：黑名单永远漏，白名单的补集才是真正的外延。
    """
    keep = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    cleaned = "".join(char if char in keep else "_" for char in raw)
    cleaned = cleaned.lstrip(".")
    return cleaned or "report"


def default_filename(window: ReportWindow, fmt: ReportFormat) -> str:
    return f"report-{window.start.isoformat()}-{window.end.isoformat()}.{fmt.value}"
