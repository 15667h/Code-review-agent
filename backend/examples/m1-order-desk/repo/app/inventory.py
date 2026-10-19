"""库存预占与释放。

预占必须是原子读改写，否则两个并发下单会同时看到「还剩 1 件」。
本模块把「重试若干次乐观锁」这一点封装起来，调用方只需要关心结果。
"""

from __future__ import annotations

from typing import Iterable

from .models import OrderItem, OutOfStock
from .repository import StockRepository

MAX_RETRY = 3


class InventoryService:
    def __init__(self, stocks: StockRepository) -> None:
        self.stocks = stocks

    def reserve(self, items: Iterable[OrderItem]) -> list[str]:
        """按明细逐行预占。返回已经预占成功的明细（供失败时原样释放）。"""
        reserved: list[OrderItem] = []
        for item in items:
            if not self._reserve_one(item.sku, item.quantity):
                self.release_items(reserved)
                raise OutOfStock(f"{item.sku} 库存不足")
            reserved.append(item)
        return [item.sku for item in reserved]

    def _reserve_one(self, sku: str, quantity: int) -> bool:
        for _ in range(MAX_RETRY):
            if self.stocks.try_reserve(sku, quantity):
                return True
        return False

    def release_items(self, items: Iterable[OrderItem]) -> None:
        for item in items:
            self.stocks.release(item.sku, item.quantity)

    def release(self, sku: str, quantity: int) -> None:
        """单行释放，供部分退款按明细回滚使用。"""
        self.stocks.release(sku, quantity)
