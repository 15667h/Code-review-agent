"""报表聚合服务。

上游是若干租户的数据源（订单库、支付流、广告投放），本服务负责：
按租户与日期范围聚合、生成定时报表、导出为 CSV / JSON 落盘。

包内所有外部系统都有本地替身，导入时不建立任何连接。
"""

from .pipeline import ReportPipeline
from .sources import DataSourceRegistry
from .export import Exporter

__all__ = ["ReportPipeline", "DataSourceRegistry", "Exporter"]

__version__ = "1.9.0"
