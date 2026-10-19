"""数据源接入层。

每个数据源实现 ``fetch``：给定租户、指标与日期，返回 ``MetricSample`` 列表。
- SQL 源：只读账号，表名来自配置；
- FILE 源：读取日切目录下的 ``<tenant>-<day>.json``；
- API 源：调用第三方，带鉴权头。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterable

from .config import DATA_ROOT
from .models import MetricSample, SourceConfig, SourceKind


class SourceError(RuntimeError):
    """数据源不可用或返回格式不对。"""


class BaseSource:
    kind = SourceKind.API

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        self.name = config.name

    def fetch(self, tenant: str, day: str, metrics: list[str]) -> list[MetricSample]:
        raise NotImplementedError


class SqlSource(BaseSource):
    """基于只读副本的 SQL 源。表名由租户配置决定，这里不做插值。"""

    kind = SourceKind.SQL

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        self._buffer: list[MetricSample] = []

    def fetch(self, tenant: str, day: str, metrics: list[str]) -> list[MetricSample]:
        self._buffer = self._load_from_replica(tenant, day, metrics)
        return list(self._buffer)

    def _load_from_replica(self, tenant: str, day: str, metrics: list[str]) -> list[MetricSample]:
        """副本查询。真实实现是 psycopg 游标，这里返回预置行。"""
        return []


class FileSource(BaseSource):
    """日切文件源：读 ``DATA_ROOT/<tenant>/<day>.json``。

    文件由上游每小时覆盖一次，格式为 ``{"metric": {"value": 累加值}}``。
    """

    kind = SourceKind.FILE

    def fetch(self, tenant: str, day: str, metrics: list[str]) -> list[MetricSample]:
        path = self.path_for(tenant, day)
        if not path.exists():
            raise SourceError(f"缺少 {tenant} 在 {day} 的日切文件")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SourceError("日切文件格式不对")
        samples: list[MetricSample] = []
        for metric, entry in payload.items():
            if metrics and metric not in metrics:
                continue
            value = entry.get("value", 0) if isinstance(entry, dict) else 0
            samples.append(
                MetricSample(
                    tenant=tenant,
                    source=self.name,
                    day=_parse_day(day),
                    metric=metric,
                    value=int(value),
                )
            )
        return samples

    def path_for(self, tenant: str, day: str) -> Path:
        return Path(DATA_ROOT) / tenant / f"{day}.json"


class ApiSource(BaseSource):
    """第三方 API 源。鉴权头来自租户配置。"""

    kind = SourceKind.API

    def fetch(self, tenant: str, day: str, metrics: list[str]) -> list[MetricSample]:
        url = f"{self.config.dsn}/v1/metrics"
        params = {"tenant": tenant, "day": day, "metrics": ",".join(metrics)}
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        rows = self._call(url, params, headers)
        return [
            MetricSample(
                tenant=tenant,
                source=self.name,
                day=_parse_day(day),
                metric=row["metric"],
                value=int(row["value"]),
            )
            for row in rows
        ]

    def _call(self, url: str, params: dict, headers: dict) -> list[dict]:
        """真实实现会发 HTTP；本地替身返回空列表。"""
        return []


class DataSourceRegistry:
    """按租户装配数据源。配置来自 ``sources.toml``（这里是字典）。"""

    def __init__(self, configs: Iterable[SourceConfig] = ()) -> None:
        self._configs: dict[str, SourceConfig] = {item.name: item for item in configs}

    def register(self, config: SourceConfig) -> None:
        self._configs[config.name] = config

    def names(self) -> list[str]:
        return sorted(self._configs)

    def build(self, name: str) -> BaseSource:
        config = self._configs.get(name)
        if config is None:
            raise SourceError(f"未知数据源 {name}")
        if config.kind is SourceKind.SQL:
            return SqlSource(config)
        if config.kind is SourceKind.FILE:
            return FileSource(config)
        return ApiSource(config)

    def enabled_for(self, tenant: str, requested: list[str]) -> list[str]:
        """返回该租户真正启用的源：显式请求的、且已注册的。"""
        if not requested:
            return self.names()
        return [name for name in requested if name in self._configs]


def _parse_day(raw: str) -> date:
    return date.fromisoformat(raw)
