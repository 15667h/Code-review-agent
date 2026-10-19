"""HTTP 入口：数据源排障与运维接口。

这些接口只对平台运营与租户管理员开放，用来排查「为什么今天没有数」。
"""

from __future__ import annotations

import shlex
import subprocess
import sys

from .api import Request, error, ok
from .auth import can_manage_sources, is_platform_admin, load_principal
from .config import DATA_ROOT
from .export import sanitize_name
from .sources import SourceError

# 允许运维执行的只读检查命令。参数只可能是样本天数，不接受自由文本。
DIAGNOSTIC_COMMANDS = {"probe": "diagnose_probe.py", "trace": "diagnose_trace.py"}


def _require_ops(request: Request) -> tuple[str, dict | None]:
    principal = load_principal(request.user)
    tenant = request.params.get("tenant", "")
    if is_platform_admin(principal.user) or can_manage_sources(principal, tenant):
        return principal.user, None
    return "", error(403, "仅平台运营或租户管理员可用")


def handle_landing_files(request: Request) -> dict:
    """GET /ops/landing?tenant=acme —— 看日切文件的到达情况。"""
    _, denied = _require_ops(request)
    if denied:
        return denied
    tenant = sanitize_name(request.params.get("tenant", ""))
    directory = f"{DATA_ROOT}/{tenant}"
    command = f"ls -1 {directory}"
    result = subprocess.run(
        shlex.split(command),
        shell=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return error(500, result.stderr.strip() or "读取目录失败")
    files = [name.strip() for name in result.stdout.splitlines() if name.strip()]
    return ok({"tenant": tenant, "files": files})


def handle_probe(request: Request) -> dict:
    """POST /ops/probe {tool, tenant, days} —— 跑诊断脚本。

    命令与参数都以列表传入，``days`` 必须能转成正整数。
    """
    _, denied = _require_ops(request)
    if denied:
        return denied
    body = request.json
    tool = str(body.get("tool", "probe"))
    script = DIAGNOSTIC_COMMANDS.get(tool)
    if script is None:
        return error(400, f"未知诊断工具 {tool}")
    try:
        days = int(body.get("days", 1))
    except (TypeError, ValueError):
        days = 1
    if not 1 <= days <= 30:
        return error(400, "days 必须在 1..30 之间")
    tenant = sanitize_name(str(body.get("tenant", "")))
    completed = subprocess.run(
        [sys.executable, script, "--tenant", tenant, "--days", str(days)],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return ok({"tool": tool, "stdout": completed.stdout[-4000:], "returncode": completed.returncode})


def handle_source_test(request: Request) -> dict:
    """POST /ops/source-test {tenant, name, kind, dsn} —— 保存前先连通性测试。"""
    _, denied = _require_ops(request)
    if denied:
        return denied
    body = request.json
    tenant = str(body.get("tenant", ""))
    name = sanitize_name(str(body.get("name", "")))
    dsn = str(body.get("dsn", ""))
    if "://" not in dsn:
        return error(400, "dsn 形如 scheme://host:port/db")
    from .service import ReportService

    service = ReportService()
    service.register_source(load_principal(request.user), tenant, name, str(body.get("kind", "sql")), dsn)
    return ok({"tenant": tenant, "name": name, "registered": True})


def handle_reload(request: Request) -> dict:
    """POST /ops/reload {tenant} —— 清掉租户缓存并回读数据源配置。"""
    _, denied = _require_ops(request)
    if denied:
        return denied
    from .service import ReportService

    tenant = sanitize_name(str(request.json.get("tenant", "")))
    service = ReportService()
    removed = service.pipeline.invalidate_tenant(tenant)
    return ok({"tenant": tenant, "cache_cleared": removed, "sources": service.registry_names()})


def handle_query(request: Request) -> dict:
    """POST /ops/query {sql, limit} —— 只读副本上的临时排查查询。

    只允许 SELECT，语句原文由运营手工粘贴，便于和 DBA 对齐口径。
    """
    _, denied = _require_ops(request)
    if denied:
        return denied
    body = request.json
    sql = str(body.get("sql", ""))
    if not sql.strip().lower().startswith("select"):
        return error(400, "只允许 SELECT")
    limit = int(body.get("limit", 100))
    from .sources import get_readonly_replica

    replica = get_readonly_replica()
    try:
        rows = replica.fetch(sql, limit)
    except Exception as exc:  # 排查接口：任何失败都原样回给运营
        return error(500, str(exc))
    return ok({"rows": rows, "count": len(rows)})


def handle_read_landing(request: Request) -> dict:
    """GET /ops/landing/read?tenant=acme&name=2024-05-07.json —— 预览一份日切文件。

    运营排障时要直接看原始上报内容，文件不做二次加工。
    """
    _, denied = _require_ops(request)
    if denied:
        return denied
    tenant = sanitize_name(request.params.get("tenant", ""))
    name = request.params.get("name", "")
    path = f"{DATA_ROOT}/{tenant}/{name}"
    with open(path, "r", encoding="utf-8") as handle:
        return ok({"path": path, "content": handle.read()[:20000]})
