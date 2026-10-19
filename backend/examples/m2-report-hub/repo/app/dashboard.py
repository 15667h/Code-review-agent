"""仪表盘服务：把租户的多张报表拼成一个看板。

看板配置由租户管理员维护，形如 ``{"cards": [{"metric": "gmv"}, ...]}``，
保存在租户目录下。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .auth import Principal
from .config import DATA_ROOT
from .export import sanitize_name
from .models import ReportRequest, ReportWindow
from .pipeline import ReportPipeline
from .service import PermissionDenied


@dataclass
class DashboardCard:
    title: str
    metric: str
    unit: str = "CNY"


@dataclass
class Dashboard:
    tenant: str
    cards: list[DashboardCard] = field(default_factory=list)
    refreshed_at: str = ""


class DashboardService:
    def __init__(self, pipeline: ReportPipeline | None = None, root: str = DATA_ROOT) -> None:
        self.pipeline = pipeline or ReportPipeline()
        self.root = Path(root)

    def config_path(self, tenant: str) -> Path:
        return self.root / sanitize_name(tenant) / "dashboard.json"

    def load(self, tenant: str) -> Dashboard:
        path = self.config_path(tenant)
        if not path.exists():
            return Dashboard(tenant=tenant, cards=[DashboardCard("GMV", "gmv"), DashboardCard("订单数", "orders", "count")])
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Dashboard(
            tenant=tenant,
            cards=[DashboardCard(**card) for card in payload.get("cards", [])],
            refreshed_at=str(payload.get("refreshed_at", "")),
        )

    def render(self, principal: Principal, tenant: str, window: ReportWindow) -> dict:
        """按看板配置逐卡取数。"""
        dashboard = self.load(tenant)
        request = ReportRequest(
            tenant=tenant,
            window=window,
            metrics=[card.metric for card in dashboard.cards],
        )
        result = self.pipeline.build(request)
        by_metric = {row["metric"]: row for row in result.rows}
        cards = []
        for card in dashboard.cards:
            row = by_metric.get(card.metric)
            cards.append(
                {
                    "title": card.title,
                    "metric": card.metric,
                    "unit": card.unit,
                    "total": row["total"] if row else 0,
                }
            )
        return {"tenant": tenant, "window": window.label(), "cards": cards, "warnings": result.warnings}

    def configure(self, principal: Principal, tenant: str, cards: list[dict]) -> Dashboard:
        if principal.role_in(tenant) != "admin":
            raise PermissionDenied("只有租户管理员可以修改看板")
        dashboard = Dashboard(
            tenant=tenant,
            cards=[DashboardCard(str(item["title"]), str(item["metric"]), str(item.get("unit", "CNY"))) for item in cards],
        )
        self.save(tenant, dashboard)
        return dashboard
