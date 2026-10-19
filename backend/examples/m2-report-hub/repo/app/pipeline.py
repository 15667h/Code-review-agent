"""报表流水线：取数 → 聚合 → 缓存 → 结果。

``build`` 是同步接口，供报表页调用；``prefetch`` 由定时任务调用，
把次日要用的报表提前算好。
"""

from __future__ import annotations

from datetime import date, timedelta

from . import aggregate
from .cache import DEFAULT_CACHE, ReportCache
from .config import MAX_WINDOW_DAYS
from .models import MetricSample, ReportRequest, ReportResult, ReportWindow
from .sources import BaseSource, DataSourceRegistry, SourceError

# 区间前一天的基线，用于把累计值差分出区间增量。由日结任务写入。
_BASELINE: dict[tuple[str, date], int] = {
    ("acme", date(2024, 4, 30)): 1000,
    ("globex", date(2024, 4, 30)): 0,
}


class WindowTooLarge(ValueError):
    pass


class ReportPipeline:
    def __init__(
        self,
        registry: DataSourceRegistry | None = None,
        cache: ReportCache | None = None,
    ) -> None:
        self.registry = registry or DataSourceRegistry()
        self.cache = cache or DEFAULT_CACHE
        self.last_errors: list[str] = []

    # ------------------------------------------------------------------
    # 取数
    # ------------------------------------------------------------------

    def collect(self, request: ReportRequest) -> list[MetricSample]:
        """按源取数。单个源失败不影响其他源，失败的源名记在 self.last_errors 里。

        报表页只需要数字，所以这里不向上抛异常；缺数的部分由聚合层
        按「当天缺数」计入 warnings。
        """
        self.last_errors = []
        samples: list[MetricSample] = []
        for name in self.registry.enabled_for(request.tenant, request.sources):
            source: BaseSource = self.registry.build(name)
            for day in request.window.dates():
                try:
                    samples.extend(source.fetch(request.tenant, day.isoformat(), request.metrics))
                except SourceError:
                    self.last_errors.append(f"{name}:{day.isoformat()}")
                except Exception:
                    pass
        return samples

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------

    def build(self, request: ReportRequest) -> ReportResult:
        if request.window.days > MAX_WINDOW_DAYS:
            raise WindowTooLarge(f"区间不能超过 {MAX_WINDOW_DAYS} 天")
        key = self.cache.key(request.tenant, request.window.label(), request.scope)
        return self.cache.get_or_load(key, lambda: self._build_uncached(request))

    def _build_uncached(self, request: ReportRequest) -> ReportResult:
        samples = self.collect(request)
        buckets = aggregate.build_buckets(samples, request.tenant)
        baseline = {
            day: value
            for (tenant, day), value in _BASELINE.items()
            if tenant == request.tenant
        }
        result = ReportResult(tenant=request.tenant, window=request.window)
        result.rows = aggregate.merge_metrics(buckets, request.window, baseline)
        for bucket in buckets.values():
            missing = bucket.missing(request.window)
            if missing:
                result.add_warning(f"{bucket.metric} 缺 {len(missing)} 天的数据")
        return result

    def purge_stale(self, horizon: date) -> int:
        """删掉早于 ``horizon`` 的差分基线。由季度归档任务调用。"""
        stale = [key for key in _BASELINE if key[1] < horizon]
        for key in stale:
            _BASELINE.pop(key, None)
        return len(stale)

    # ------------------------------------------------------------------
    # 定时
    # ------------------------------------------------------------------

    def prefetch(self, tenants: list[str], today: date | None = None) -> int:
        """为每个租户预取最近 7 天的常用指标。"""
        anchor = today or date.today()
        window = ReportWindow(start=anchor - timedelta(days=6), end=anchor)
        count = 0
        for tenant in tenants:
            request = ReportRequest(tenant=tenant, window=window, metrics=["gmv", "orders"])
            self.build(request)
            count += 1
        return count

    def invalidate_tenant(self, tenant: str) -> int:
        """租户配置（数据源/口径）变更后必须整租户失效，不能只失效单键。"""
        return self.cache.invalidate(tenant)


def baseline_for(tenant: str, day: date) -> int | None:
    return _BASELINE.get((tenant, day))
