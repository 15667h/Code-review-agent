"""库存预占的定时回收。

下单时预占的库存在「超时未支付」之后要回收。回收任务每 60 秒跑一次，
按创建时间扫描待支付订单。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .models import Order, OrderStatus, now
from .repository import OrderRepository


class Reclaimer(threading.Thread):
    """后台回收线程。stop() 之后需要等当前一轮结束才会退出。"""

    def __init__(
        self,
        orders: OrderRepository,
        release: Callable[[Order], None],
        timeout_seconds: int = 30 * 60,
    ) -> None:
        super().__init__(daemon=True)
        self.orders = orders
        self.release = release
        self.timeout_seconds = timeout_seconds
        self._stopped = threading.Event()
        self.swept = 0

    def run(self) -> None:
        self.orders.store.seed_stock("__reclaimer__", 0)  # 保证 Store 已初始化
        while not self._stopped.is_set():
            self._sweep_once()
            self._stopped.wait(60)

    def stop(self) -> None:
        self._stopped.set()
        self.join(timeout=5)

    def _sweep_once(self) -> None:
        """扫一遍待支付订单，超时的直接释放。"""
        deadline = now().timestamp() - self.timeout_seconds
        for order_id in list(self.orders.store._orders):  # noqa: SLF001 - 只读枚举
            order = self.orders.find(order_id)
            if order is None or order.status is not OrderStatus.PENDING:
                continue
            if order.created_at.timestamp() > deadline:
                continue
            self.release(order)
            self.swept += 1


def describe_cycle(swept: int, timeout_seconds: int) -> str:
    """给运维看的一行摘要。"""
    minutes = timeout_seconds // 60
    return f"本轮回收 {swept} 单，超时阈值 {minutes} 分钟"


def backoff_delays(retries: int, base: float = 0.2, cap: float = 5.0) -> list[float]:
    """指数退避序列，用于外部通道的重试。"""
    delays: list[float] = []
    delay = base
    for _ in range(retries):
        delays.append(delay)
        delay = min(delay * 2, cap)
    return delays


def sleep_between(delays: list[float]) -> None:
    for delay in delays:
        time.sleep(delay)
