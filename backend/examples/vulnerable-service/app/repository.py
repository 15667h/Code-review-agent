"""示例模块：数据访问层，故意包含注入与异常处理缺陷。

标注写在缺陷行的行尾，格式 `# 缺陷 编号：说明`。
"""


def find_user(conn, name: str):
    """按用户名查询。"""
    sql = "SELECT id, name FROM users WHERE name = '" + name + "'"  # 缺陷 SEC002-a：外部输入直接拼进 SQL
    return conn.execute(sql)


def list_orders(conn, status: str, limit: int):
    """按状态列出订单。"""
    sql = f"SELECT id FROM orders WHERE status = '{status}' LIMIT {limit}"  # 缺陷 SEC002-b：f-string 拼接同样构成注入
    return conn.execute(sql)


def delete_session(conn, token: str) -> None:
    """删除会话记录。"""
    try:
        conn.execute("DELETE FROM sessions WHERE token = %s", (token,))
    except Exception:  # 缺陷 ERR002-a：异常被静默吞掉，调用方无法感知失败
        pass


def sync_ledger(conn, rows):
    """把一批流水写入账本。"""
    try:
        for row in rows:
            conn.execute("INSERT INTO ledger VALUES (%s, %s)", row)
    except:  # 缺陷 ERR001-b：裸 except 会连 KeyboardInterrupt 一起吞掉
        return False
    return True
