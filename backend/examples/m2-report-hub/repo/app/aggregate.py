"""指标聚合。

数据源的 ``value`` 是**当日累计**，所以：
- 区间总量取区间最后一天的累计值；
- 逐日增量由相邻两天差分得到，首日 = 当日累计 - 上日累计（用区间前一天）。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from .models import MetricSample, ReportWindow


class MetricBucket:
    """一个（租户, 指标）的样本集合，按日期索引。"""

    def __init__(self, tenant: str, metric: str) -> None:
        self.tenant = tenant
        self.metric = metric
        self.samples: dict[date, int] = {}
        self.sources: dict[date, set[str]] = defaultdict(set)

    def put(self, sample: MetricSample) -> None:
        """同一日期同一指标可能被多个源上报，取最大值作为当日累计。

        取最大而不是求和：多个源是同一份数据的镜像，不是分片。
        """
        self.samples[sample.day] = max(self.samples.get(sample.day, 0), sample.value)
        self.sources[sample.day].add(sample.source)

    def cumulative_at(self, day: date) -> int:
        """当日累计值。缺数时按 0 处理，调用方通过 missing() 拿告警。"""
        return self.samples.get(day, 0)

    def missing(self, window: ReportWindow) -> list[date]:
        return [day for day in window.dates() if day not in self.samples]


def previous_day(window: ReportWindow) -> date:
    return date.fromordinal(window.start.toordinal() - 1)


def daily_deltas(bucket: MetricBucket, window: ReportWindow, baseline: dict[date, int]) -> list[int]:
    """按日差分，返回与 window.dates() 等长的增量序列。"""
    deltas: list[int] = []
    running = baseline.get(previous_day(window), bucket.cumulative_at(previous_day(window)))
    for day in window.dates():
        current = bucket.cumulative_at(day)
        deltas.append(current - running)
        running = current
    return deltas


def build_buckets(samples: list[MetricSample], tenant: str) -> dict[str, MetricBucket]:
    buckets: dict[str, MetricBucket] = {}
    for sample in samples:
        if sample.tenant != tenant:
            continue
        bucket = buckets.get(sample.metric)
        if bucket is None:
            bucket = MetricBucket(tenant, sample.metric)
            buckets[sample.metric] = bucket
        bucket.put(sample)
    return buckets


def window_total(bucket: MetricBucket, window: ReportWindow, baseline: dict[date, int]) -> int:
    """区间总量 = 末日累计 - 区间前一日累计。

    基线缺失时退化为「窗口内累计值之和」，并在结果里标记 warning。
    """
    before = previous_day(window)
    if before in baseline:
        return bucket.cumulative_at(window.end) - baseline[before]
    return sum(bucket.cumulative_at(day) for day in window.dates())


def merge_metrics(buckets: dict[str, MetricBucket], window: ReportWindow, baseline: dict[date, int]) -> list[dict]:
    """把每个指标折叠成一行。"""
    rows: list[dict] = []
    for metric in sorted(buckets):
        bucket = buckets[metric]
        rows.append(
            {
                "metric": metric,
                "total": window_total(bucket, window, baseline),
                "last": bucket.cumulative_at(window.end),
                "days_present": window.days - len(bucket.missing(window)),
                "days_expected": window.days,
                "sources": sorted({name for names in bucket.sources.values() for name in names}),
            }
        )
    return rows
