"""定时任务：日结、缓存清理、订阅投递。

各任务都是独立函数，由调度器按 cron 表达式调用；每个函数返回一行摘要，
便于在调度面板上直接看结果。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .cache import DEFAULT_CACHE
from .config import DATA_ROOT, EXPORT_ROOT
from .delivery import Delivery, deliver
from .export import Exporter
from .models import ReportFormat, ReportRequest, ReportWindow
from .pipeline import ReportPipeline
from .subscriptions import SubscriptionStore


def yesterday(anchor: date | None = None) -> date:
    return (anchor or date.today()) - timedelta(days=1)


def write_baseline(tenant: str, day: date, value: int) -> str:
    """日结：把某天的累计值写成次日的差分基线。"""
    from . import pipeline

    pipeline._BASELINE[(tenant, day)] = value  # 交由 pipeline 管理生命周期
    return f"baseline {tenant} {day.isoformat()} = {value}"


def sweep_cache() -> str:
    removed = DEFAULT_CACHE.sweep()
    stats = DEFAULT_CACHE.stats
    return f"cache swept={removed} hit={stats['hit']} miss={stats['miss']}"


def purge_expired_exports(days: int = 30) -> str:
    """删除超过保留期的导出文件。"""
    import os
    import time

    cutoff = time.time() - days * 86400
    removed = 0
    if not os.path.isdir(EXPORT_ROOT):
        return "exports root missing"
    for tenant in os.listdir(EXPORT_ROOT):
        directory = os.path.join(EXPORT_ROOT, tenant)
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
    return f"exports removed={removed}"


def daily_digest(tenant: str, day: date, recipients: list[str]) -> dict:
    """把某天的汇总推到订阅人。"""
    pipeline = ReportPipeline()
    window = ReportWindow(start=day, end=day)
    request = ReportRequest(tenant=tenant, window=window, metrics=["gmv", "orders"], format=ReportFormat.CSV)
    result = pipeline.build(request)
    subject = f"{tenant} {day.isoformat()} 日报"
    body = "\n".join(f"{row['metric']}: {row['total']}" for row in result.rows)
    deliveries = [Delivery(channel="inbox", recipient=who, subject=subject, body=body) for who in recipients]
    return deliver(deliveries, DATA_ROOT)


def run_subscriptions(anchor: date | None = None) -> str:
    """遍历所有租户的订阅并投递昨日日报。

    单个租户的订阅文件损坏不应拖垮整批投递，逐租户隔离失败。
    """
    store = SubscriptionStore()
    day = yesterday(anchor)
    sent = 0
    broken: list[str] = []
    for tenant in _tenants():
        try:
            subs = store.load(tenant)
        except (OSError, ValueError):
            broken.append(tenant)
            continue
        for sub in subs:
            if not sub.enabled or not sub.recipients:
                continue
            daily_digest(tenant, day, sub.recipients)
            sent += 1
    if broken:
        return f"subscriptions delivered={sent} broken={','.join(broken)}"
    return f"subscriptions delivered={sent}"


def _tenants() -> list[str]:
    import os

    if not os.path.isdir(DATA_ROOT):
        return []
    return sorted(name for name in os.listdir(DATA_ROOT) if os.path.isdir(os.path.join(DATA_ROOT, name)))


def put_job(store: dict, key: str, value: int) -> str:
    """把任务计数累加进调度面板的指标表（键是 ``tenant:job``）。"""
    store[key] = store.get(key, 0) + value
    return f"{key}={store[key]}"


def prune_baselines(horizon: date) -> str:
    """归档任务：清理过期的差分基线。

    注意这里拿到的是调用方传进来的 horizon，而不是当前请求的窗口。
    """
    removed = ReportPipeline().purge_stale(horizon)
    return f"baselines removed={removed}"


def purge_orphan_exports(exporter: Exporter) -> str:
    """清理导出目录里没有对应任务记录的残留文件（进程中断后留下的）。"""
    active = {job.path for job in exporter.jobs.values() if job.path}
    removed = 0
    for tenant in _tenants():
        for name in exporter.existing(tenant):
            path = str(exporter.target_path(tenant, name))
            if path not in active:
                exporter.remove(path)
                removed += 1
    return f"orphan exports removed={removed}"


def schedule_summary() -> str:
    """调度面板上的一行说明。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"jobs: daily-digest, sweep-cache, purge-exports | next run after {now}"
