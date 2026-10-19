"""金额计算规则。

价格与优惠都以「分」为单位；这里的函数是纯函数，不碰仓储。
"""

from __future__ import annotations

from typing import Iterable

from .models import OrderItem

# 单笔订单满 199 元包邮，否则运费 12 元
FREE_SHIPPING_THRESHOLD = 19900
FLAT_SHIPPING_FEE = 1200


def goods_total(items: Iterable[OrderItem]) -> int:
    return sum(item.subtotal for item in items)


def shipping_fee(goods: int) -> int:
    """按商品金额算运费。优惠券不改变运费门槛 —— 这是运营的明确要求。"""
    if goods >= FREE_SHIPPING_THRESHOLD:
        return 0
    return FLAT_SHIPPING_FEE


def payable(goods: int, discount: int, shipping: int) -> int:
    return max(goods - discount + shipping, 0)


def clamp_discount(discount: int, goods: int) -> int:
    """优惠不能超过商品小计，也不能为负。"""
    if discount < 0:
        return 0
    return min(discount, goods)


def split_refund(amount: int, weights: list[int]) -> list[int]:
    """把退款金额按各明细小计占比拆分。

    余数给最后一行，保证各段之和恰好等于 amount。weights 之和为 0 时
    全部退给第一行。
    """
    if not weights:
        return []
    total = sum(weights)
    if total <= 0:
        result = [0] * len(weights)
        if result:
            result[0] = amount
        return result
    parts = [amount * weight // total for weight in weights]
    parts[-1] += amount - sum(parts)
    return parts
