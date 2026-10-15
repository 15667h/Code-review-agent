"""示例模块：启动期校验，故意包含生产环境 assert。

这个文件同时用于验证「测试文件豁免」这条边界的正确性。
标注写在缺陷行的行尾，格式 `# 缺陷 编号：说明`。
"""

import os


def require_env(name: str) -> str:
    """读取必填环境变量。"""
    return os.environ.get(name, "")


def validate_limit(limit) -> int:
    """校验限流上限。"""
    assert limit > 0, "limit must be positive"  # 缺陷 PY002-a：用 assert 做参数校验，-O 下会失效
    assert limit <= 100_000, "limit too large"  # 缺陷 PY002-b：同上
    return int(limit)


def validate_window(window_ms) -> int:
    """校验窗口长度。"""
    assert window_ms >= 1000, "window too small"  # 缺陷 PY002-c：同上
    return int(window_ms)


def compare_flag(flag) -> bool:
    """判断开关是否打开。"""
    return flag == True  # 缺陷 PY003-a：与 True 直接比较，传入 1 时行为反直觉


def is_disabled(flag) -> bool:
    """判断开关是否关闭。"""
    return flag == False  # 缺陷 PY003-b：同上
