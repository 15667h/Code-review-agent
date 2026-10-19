"""运行配置。

所有值都可以被环境变量覆盖，前缀 ``REPORT_HUB_``。
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _env(name: str, default: str) -> str:
    return os.environ.get(f"REPORT_HUB_{name}", default)


# 日切文件与导出文件的根目录
DATA_ROOT = _env("DATA_ROOT", str(DEFAULT_ROOT / "data"))
EXPORT_ROOT = _env("EXPORT_ROOT", str(DEFAULT_ROOT / "exports"))

# 报表缓存：同一份请求 10 分钟内直接命中
CACHE_TTL_SECONDS = int(_env("CACHE_TTL", "600"))
MAX_WINDOW_DAYS = int(_env("MAX_WINDOW_DAYS", "120"))
MAX_ROWS = int(_env("MAX_ROWS", "100000"))

# 导出任务完成后回调上游的地址，由部署时注入
CALLBACK_URL = _env("CALLBACK_URL", "http://report-hub.internal:9000/callback")
