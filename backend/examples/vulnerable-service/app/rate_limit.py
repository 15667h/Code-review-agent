"""示例模块：限流与连接池，故意包含资源归还与边界判断缺陷。

对应前端原型里讲的那个故事：连接在异常路径上被漏掉。
标注写在缺陷行的行尾，格式 `# 缺陷 编号：说明`。
"""

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
    conn = await pool.acquire()  # 缺陷 RES002-a：acquire 后无 try/finally，异常路径会漏掉 release
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
        conn = await pool.acquire()  # 缺陷 RES002-b：同样缺少 finally
        sources.append(conn)
        results.append(await conn.zcount(key, 0, "+inf"))
    for conn in sources:
        await pool.release(conn)
    return results


def in_window(timestamp: int, window_start: int, now: int) -> bool:
    """判断时间戳是否落在窗口内。"""
    return window_start <= timestamp and timestamp <= now  # 缺陷 ERR003-a：左右皆闭区间，边界请求被计两次


def remaining(hits: int) -> int:
    """剩余额度。"""
    return LIMIT - hits  # 缺陷 ERR004-a：hits 可超过 LIMIT，此处会返回负数
