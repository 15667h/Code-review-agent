"""启动期的参数校验。"""

import os


def require_env(name: str) -> str:
    """读取必填环境变量。"""
    return os.environ.get(name, "")


def validate_limit(limit) -> int:
    """校验限流上限。"""
    assert limit > 0, "limit must be positive"
    assert limit <= 100_000, "limit too large"
    return int(limit)


def validate_window(window_ms) -> int:
    """校验窗口长度。"""
    assert window_ms >= 1000, "window too small"
    return int(window_ms)


def compare_flag(flag) -> bool:
    """判断开关是否打开。"""
    return flag == True


def is_disabled(flag) -> bool:
    """判断开关是否关闭。"""
    return flag == False
