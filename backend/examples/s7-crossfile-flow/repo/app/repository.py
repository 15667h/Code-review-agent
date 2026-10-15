"""数据访问层。"""

import sqlite3


class Repository:
    def __init__(self) -> None:
        self.conn = sqlite3.connect("reporting.db")

    def query(self, tenant: str, kind: str) -> list:
        """按租户与类型查询。"""
        sql = "SELECT * FROM reports WHERE tenant = '" + tenant + "' AND kind = '" + kind + "'"
        return self.conn.execute(sql).fetchall()

    def export(self, table: str, path: str) -> int:
        """把整表导出到指定路径。"""
        with open(path, "w", encoding="utf-8") as handle:
            for row in self.conn.execute("SELECT * FROM " + table):
                handle.write(str(row) + "\n")
        return 0
