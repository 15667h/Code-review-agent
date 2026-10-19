"""报表领域的实体。

指标口径：
- ``MetricSample.value`` 是**累计值**（每个数据源上报的是当日累计，不是增量）；
- ``ReportWindow`` 的区间是左闭右闭，与业务方给的「从 5 月 1 日到 5 月 7 日」一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum


class SourceKind(str, Enum):
    """数据源类型。SQL 类走只读账号，FILE 类读取落盘的日切文件。"""

    SQL = "sql"
    FILE = "file"
    API = "api"


class ReportFormat(str, Enum):
    CSV = "csv"
    JSON = "json"


@dataclass(frozen=True)
class ReportWindow:
    """日期区间，左闭右闭。"""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("结束日期不能早于开始日期")
        if (self.end - self.start).days > 366:
            raise ValueError("单个窗口不能超过一年")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def dates(self) -> list[date]:
        return [date.fromordinal(self.start.toordinal() + offset) for offset in range(self.days)]

    def label(self) -> str:
        return f"{self.start.isoformat()}~{self.end.isoformat()}"

@dataclass(frozen=True)
class MetricSample:
    """一行上报数据。value 为当日累计值。"""

    tenant: str
    source: str
    day: date
    metric: str
    value: int



@dataclass
class SourceConfig:
    """租户配置下的一个数据源。"""

    name: str
    kind: SourceKind
    dsn: str
    table: str = ""
    api_key: str = ""


@dataclass
class ReportRequest:
    """一次报表请求。"""

    tenant: str
    window: ReportWindow
    metrics: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    format: ReportFormat = ReportFormat.JSON
    filename: str = "report"
    requested_by: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def scope(self) -> str:
        """同一租户下「看哪些源、哪些指标」的口径标签。"""
        return f"{','.join(sorted(self.metrics))}/{','.join(sorted(self.sources))}"


@dataclass
class ReportResult:
    """聚合结果。rows 的键是（日期, 指标名）。"""

    tenant: str
    window: ReportWindow
    rows: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(int(row.get("value", 0)) for row in self.rows)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)
