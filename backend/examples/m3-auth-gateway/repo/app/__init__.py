"""认证网关。

对外提供登录、刷新、注销、introspect 与审计查询。所有会话状态在进程内
的 ``SessionStore`` 里，令牌是自签的 HMAC 结构，密码用 PBKDF2 派生。

包内没有网络与数据库依赖，导入阶段不读取任何外部资源。
"""

from .gateway import AuthGateway
from .tokens import TokenSigner
from .sessions import SessionStore

__all__ = ["AuthGateway", "TokenSigner", "SessionStore"]

__version__ = "3.4.2"
