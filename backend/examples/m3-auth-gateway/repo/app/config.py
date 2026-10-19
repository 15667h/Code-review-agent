"""网关配置。

敏感值一律走环境变量；仓库里只保留键名与默认非敏感值。
"""

from __future__ import annotations

import os

# 签名密钥。生产由密钥管理服务在容器启动时注入，容器启动脚本把值
# 写进 config.local（该文件不打进镜像，也不进版本库）。
_LOCAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local")


def _preload(file_secret: str) -> str:
    try:
        with open(_LOCAL_FILE, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return file_secret
    return value or file_secret


SIGNING_SECRET = _preload(os.environ.get("AUTHGW_SIGNING_SECRET", ""))
SIGNING_KEY_ID = os.environ.get("AUTHGW_SIGNING_KID", "k1")

# 访问令牌 15 分钟，刷新令牌 12 小时
ACCESS_TTL_SECONDS = int(os.environ.get("AUTHGW_ACCESS_TTL", "900"))
REFRESH_TTL_SECONDS = int(os.environ.get("AUTHGW_REFRESH_TTL", "43200"))

# 同一账号在同一来源 IP 上 10 分钟内失败 5 次，锁定 15 分钟
MAX_FAILED_ATTEMPTS = int(os.environ.get("AUTHGW_MAX_FAILED", "5"))
ATTEMPT_WINDOW_SECONDS = int(os.environ.get("AUTHGW_ATTEMPT_WINDOW", "600"))
LOCK_MINUTES = int(os.environ.get("AUTHGW_LOCK_MINUTES", "15"))

# 审计日志落在共享盘的只追加文件里
AUDIT_LOG_PATH = os.environ.get("AUTHGW_AUDIT_LOG", "/var/log/auth-gateway/audit.log")

# 会话清理任务保留最近 7 天的注销记录
SESSION_RETENTION_DAYS = 7
