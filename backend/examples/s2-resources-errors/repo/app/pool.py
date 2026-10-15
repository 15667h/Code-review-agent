"""极简连接池。"""


class Pool:
    def __init__(self, size: int = 8) -> None:
        self.size = size
        self.in_use = 0

    def acquire(self) -> "Pool":
        self.in_use += 1
        return self

    def release(self, conn: "Pool") -> None:
        self.in_use -= 1

    def count(self, key: str) -> int:
        return len(key)


def compute(pool: Pool, key: str) -> int:
    """按 key 统计命中次数。"""
    conn = pool.acquire()
    hits = conn.count(key)
    if hits > 100:
        return hits
    pool.release(conn)
    return hits
