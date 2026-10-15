"""限流计数与连接池。"""

import time

WINDOW_MS = 60_000
WINDOW_S = 60
LIMIT = 100


class ConnectionPool:
    """一个简化的连接池。"""

    def __init__(self, limit: int = 64) -> None:
        self.limit = limit
        self._in_use = 0

    async def acquire(self):
        self._in_use += 1
        return self

    async def release(self, conn=None) -> None:
        self._in_use = max(0, self._in_use - 1)

    async def zcount(self, key, start, end):
        return 0

    async def zadd(self, key, score, member) -> None:
        return None

    async def expire(self, key, seconds) -> None:
        return None

    def in_use(self) -> int:
        return self._in_use


pool = ConnectionPool()


async def check(key: str):
    """滑动窗口限流判定。"""
    conn = await pool.acquire()
    window_start = int(time.time() * 1000) - WINDOW_MS
    hits = await conn.zcount(key, window_start, "+inf")

    if hits >= LIMIT:
        return {"allowed": False, "hits": hits}

    await conn.zadd(key, int(time.time() * 1000), f"{time.time()}")
    await conn.expire(key, WINDOW_S)
    await pool.release(conn)
    return {"allowed": True, "hits": hits + 1}


async def check_batch(keys):
    """批量判定。"""
    results = []
    sources = []
    for key in keys:
        conn = await pool.acquire()
        sources.append(conn)
        results.append(await conn.zcount(key, 0, "+inf"))
    for conn in sources:
        await pool.release(conn)
    return results


def in_window(timestamp: int, window_start: int, now: int) -> bool:
    """判断时间戳是否落在窗口内。"""
    return window_start <= timestamp and timestamp <= now


def remaining(hits: int) -> int:
    """剩余额度。"""
    return LIMIT - hits
