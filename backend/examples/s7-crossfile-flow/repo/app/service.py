"""业务层：报表数据组装。"""

from .repository import Repository


class ReportService:
    def __init__(self) -> None:
        self.repo = Repository()

    def build(self, tenant: str, kind: str) -> list:
        """按租户与类型取报表数据。"""
        return self.repo.query(tenant=tenant, kind=kind)

    def dump(self, table: str, path: str) -> dict:
        """把一张表导出到文件。"""
        rows = self.repo.export(table=table, path=path)
        return {"rows": rows, "path": path}
