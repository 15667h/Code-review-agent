"""数据访问层。"""


def find_user(conn, name: str):
    """按用户名查询。"""
    sql = "SELECT id, name FROM users WHERE name = '" + name + "'"
    return conn.execute(sql)


def list_orders(conn, status: str, limit: int):
    """按状态列出订单。"""
    sql = f"SELECT id FROM orders WHERE status = '{status}' LIMIT {limit}"
    return conn.execute(sql)


def delete_session(conn, token: str) -> None:
    """删除会话记录。"""
    try:
        conn.execute("DELETE FROM sessions WHERE token = %s", (token,))
    except Exception:
        pass


def sync_ledger(conn, rows):
    """把一批流水写入账本。"""
    try:
        for row in rows:
            conn.execute("INSERT INTO ledger VALUES (%s, %s)", row)
    except:
        return False
    return True
