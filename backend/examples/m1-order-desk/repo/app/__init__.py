"""订单后台服务。

面向内部运营与前台下单页面的一组 Python 模块。生产环境里 OrderService
会被 WSGI 适配层按请求实例化，数据访问走 ``app.db`` 里的连接池。

本包内的所有外部依赖都有本地替身，导入时不会建立任何网络或磁盘连接。
"""

from .service import OrderService
from .workbench import MemoryWorkbench
from .models import Order, OrderItem, OrderStatus

__all__ = [
    "OrderService",
    "MemoryWorkbench",
    "Order",
    "OrderItem",
    "OrderStatus",
]

__version__ = "2.7.1"
