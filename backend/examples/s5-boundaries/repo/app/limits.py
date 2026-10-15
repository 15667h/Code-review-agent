"""限流窗口与分页计算。"""

from datetime import date, datetime

WINDOW_SECONDS = 60
LIMIT = 100


def in_window(ts: float, start: float, now: float) -> bool:
    """判断事件是否落在窗口内。"""
    return start <= ts and ts <= now


def remaining(hits: int) -> int:
    """还剩多少配额。"""
    return LIMIT - hits


def page_offset(page: int, size: int) -> int:
    """按页码算偏移量（页码从 1 开始）。"""
    return page * size


def percentage(part: int, whole: int) -> float:
    """计算占比。"""
    return part / whole * 100


def days_between(start: date, end: date) -> int:
    """含首尾两天的天数。"""
    return (end - start).days


def quota_key(now: datetime, minutes_west: int) -> str:
    """按客户端时区构造配额键。"""
    hour = now.hour + minutes_west // 60
    return now.strftime("%Y-%m-%d") + ":" + str(hour)
