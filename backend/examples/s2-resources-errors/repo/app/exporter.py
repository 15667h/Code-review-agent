"""报表导出。"""

import logging
import sqlite3

log = logging.getLogger(__name__)


def export_csv(path: str, rows: list) -> None:
    """把报表写成 CSV。"""
    handle = open(path, "w", encoding="utf-8")
    handle.write("id,name\n")
    for row in rows:
        handle.write(str(row["id"]) + "," + str(row["name"]) + "\n")
    handle.close()


def fetch_config(conn: sqlite3.Connection, key: str) -> dict:
    """读一条配置。"""
    try:
        cursor = conn.execute("SELECT value FROM config WHERE key = ?", (key,))
        return {"value": cursor.fetchone()[0]}
    except:
        return {}


def purge_cache(conn: sqlite3.Connection) -> int:
    """清理过期缓存，返回删除条数。"""
    try:
        cursor = conn.execute("DELETE FROM cache WHERE expires_at < datetime('now')")
        conn.commit()
        return cursor.rowcount
    except Exception:
        pass
    return 0
