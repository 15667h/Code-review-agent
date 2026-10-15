"""HTTP 入口：报表查询与导出。"""

from .service import ReportService


def handle_query(request):
    """GET /report?tenant=<租户>&kind=<报表类型>"""
    tenant = request.args.get("tenant", "")
    kind = request.args.get("kind", "summary")
    return ReportService().build(tenant=tenant, kind=kind)


def handle_export(request):
    """POST /report/export  {table, path}"""
    table = request.json.get("table", "")
    path = request.json.get("path", "/tmp/export.txt")
    return ReportService().dump(table=table, path=path)
