"""报告渲染：Markdown 与终端两种形态。

报告只陈述**有证据的结论**；运行过程中的告警（护栏拒绝、核验失败、迭代耗尽）
会单列出来，避免把它们混进「发现」里冒充审计结论。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .loop import AuditResult
from .types import Finding

SEVERITY_LABEL = {"blocker": "阻断", "warn": "警告", "note": "建议"}
SEVERITY_ORDER = ("blocker", "warn", "note")

_RED = "\033[31m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_GREEN = "\033[32m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"

_SEVERITY_COLOR = {"blocker": _RED, "warn": _YELLOW, "note": _BLUE}


def sort_findings(findings: list[Finding]) -> list[Finding]:
    def key(item: Finding) -> tuple[int, str, int]:
        return (SEVERITY_ORDER.index(item.severity) if item.severity in SEVERITY_ORDER else 9, item.file, item.line)

    return sorted(findings, key=key)


# =============================================================================
# Markdown
# =============================================================================


# =============================================================================
# generic 兜底：前端「不认识」的 outcome 也不能在报告里消失
#
# 与前端 blueprint.js 的 createGeneric 同一职责 —— 后端注册了新 agent 而这一层
# 还没写专用渲染器时，按字段原样列出。没有它，未知 outcome 会掉进审查渲染器，
# 打印「本次没有记录任何发现」：那不是事实，是误导。
# =============================================================================

_SKIP_FIELDS = {"id", "fingerprint"}


def _generic_rows(result: AuditResult) -> list[tuple[str, list[Any]]]:
    rows: list[tuple[str, list[Any]]] = []
    for kind, items in result.artifacts.items():
        if items:
            rows.append((kind, items))
    return rows


def render_generic_markdown(result: AuditResult, *, repo: str = "", model: str = "") -> str:
    lines: list[str] = []
    lines.append("# 运行报告")
    lines.append("")
    lines.append(f"- **目标**：{result.goal}")
    if repo:
        lines.append(f"- **仓库路径**：`{repo}`")
    if model:
        lines.append(f"- **驱动模型**：`{model}`")
    lines.append(f"- **产出形态**：`{result.outcome or '（未声明）'}`")
    lines.append(f"- **迭代轮数**：{result.iterations}")
    lines.append(f"- **结束原因**：`{result.stop_reason}`")
    lines.append("")
    lines.append("> 这一形态还没有专用渲染器，以下按字段原样列出。")
    lines.append("")

    rows = _generic_rows(result)
    if not rows:
        lines.append("本次运行没有登记任何产出。")
    for kind, items in rows:
        lines.append(f"## {kind}（{len(items)} 条）")
        lines.append("")
        for item in items:
            data = item if isinstance(item, dict) else vars(item)
            for key, value in data.items():
                if key in _SKIP_FIELDS or value in (None, "", [], {}):
                    continue
                if isinstance(value, (list, tuple)):
                    rendered = "；".join(str(v) for v in value)
                else:
                    rendered = str(value)
                lines.append(f"- **{key}**：{rendered}")
            lines.append("")
    if result.summary:
        lines.append("## 总结")
        lines.append("")
        lines.append(result.summary)
    return "\n".join(lines)


def render_generic_terminal(result: AuditResult, *, color: bool = True) -> str:
    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    lines: list[str] = []
    lines.append("")
    lines.append(paint("─" * 64, _DIM))
    lines.append(paint(f"  运行完成 · 产出形态 {result.outcome or '（未声明）'}", _BOLD))
    lines.append(paint("─" * 64, _DIM))
    lines.append("")
    lines.append(f"  目标：{result.goal}")
    lines.append(f"  轮数：{result.iterations}    结束原因：{result.stop_reason or '—'}")
    rows = _generic_rows(result)
    if not rows:
        lines.append("")
        lines.append("  本次运行没有登记任何产出。")
    for kind, items in rows:
        lines.append("")
        lines.append(paint(f"  {kind}（{len(items)} 条）", _BOLD))
        for item in items:
            data = item if isinstance(item, dict) else vars(item)
            summary_bits = [
                f"{key}={value}"
                for key, value in data.items()
                if key not in _SKIP_FIELDS and value not in (None, "", [], {})
            ]
            lines.append("    · " + "，".join(summary_bits[:4]))
    if result.summary:
        lines.append("")
        lines.append(f"  总结：{result.summary}")
    lines.append("")
    return "\n".join(lines)


# outcome → 渲染器。新 agent 注册时在这里加一行即可；**没加也不会走错渲染器** ——
# 未知 outcome 落到 generic（按字段原样列），而不是掉进审查渲染器打印
# 「本次没有记录任何发现」（那不是事实，是误导，比不渲染更糟）。
_MARKDOWN_RENDERERS: dict[str, Callable[..., str]] = {
    "blueprint": lambda result, **kw: render_blueprint_markdown(result, **kw),
    "test_plan": lambda result, **kw: render_test_plan_markdown(result, **kw),
    "roadmap": lambda result, **kw: render_roadmap_markdown(result, **kw),
    "threat_model": lambda result, **kw: render_threat_model_markdown(result, **kw),
}


def render_markdown(result: AuditResult, *, repo: str = "", model: str = "") -> str:
    """按产出形态分派渲染器。"""
    renderer = _MARKDOWN_RENDERERS.get(result.outcome)
    if renderer is not None:
        return renderer(result, repo=repo, model=model)
    if result.outcome != "findings" and "finding" not in result.artifacts:
        return render_generic_markdown(result, repo=repo, model=model)

    counts = result.severity_counts
    lines: list[str] = []

    lines.append("# 代码审计报告")
    lines.append("")
    lines.append(f"- **审计目标**：{result.goal}")
    if repo:
        lines.append(f"- **仓库路径**：`{repo}`")
    if model:
        lines.append(f"- **驱动模型**：`{model}`")
    lines.append(f"- **迭代轮数**：{result.iterations}")
    lines.append(f"- **结束原因**：`{result.stop_reason}`")
    lines.append(f"- **耗时**：{result.elapsed:.1f}s")
    lines.append(
        f"- **Token**：prompt {result.usage.prompt_tokens} / completion "
        f"{result.usage.completion_tokens} / 缓存命中 {result.usage.cached_tokens}"
    )
    lines.append("")

    lines.append("## 结论")
    lines.append("")
    lines.append(result.summary.strip() or "（模型未给出文字结论）")
    lines.append("")

    ordered = sort_findings(result.findings)
    lines.append(
        f"## 发现（共 {len(ordered)} 条：阻断 {counts['blocker']} / 警告 {counts['warn']} / 建议 {counts['note']}）"
    )
    lines.append("")

    if not ordered:
        lines.append("本次运行没有记录任何发现。")
        lines.append("")
        lines.append("> 注意：「没有发现」不等于「没有问题」。请结合下方的运行提示判断覆盖是否充分。")
        lines.append("")
    else:
        for index, finding in enumerate(ordered, start=1):
            label = SEVERITY_LABEL.get(finding.severity, finding.severity)
            lines.append(f"### {index}. [{label}] {finding.title}")
            lines.append("")
            lines.append(f"- **位置**：`{finding.file}:{finding.line}`")
            lines.append(f"- **编号**：`{finding.id}`")
            lines.append(f"- **机理**：{finding.detail}")
            lines.append(f"- **证据**：{finding.evidence}")
            if finding.suggestion:
                lines.append(f"- **建议**：{finding.suggestion}")
            lines.append("")

    if result.warnings:
        lines.append("## 运行提示")
        lines.append("")
        lines.append("以下内容不是审计发现，而是本次运行的健壮性说明：")
        lines.append("")
        for warning in result.warnings:
            for line in warning.splitlines():
                lines.append(f"- {line}" if not line.startswith(("-", " ")) else f"  {line}")
        lines.append("")

    if result.compression_events:
        lines.append("## 上下文压缩")
        lines.append("")
        for event in result.compression_events:
            stats = event.get("stats", {})
            lines.append(
                f"- 第 {event['iteration']} 轮触发：压缩 {stats.get('compressed', 0)} 条工具输出，"
                f"丢弃噪声 {stats.get('dropped_noise', 0)} 条"
            )
        lines.append("")

    lines.append("## 附录")
    lines.append("")
    lines.append(f"- 静态前缀指纹：`{result.prefix_trace.get('fingerprint', 'n/a')}`")
    lines.append(f"- 静态前缀 token：约 {result.prefix_trace.get('tokens', 0)}")
    lines.append(f"- 轨迹消息数：{len(result.trajectory)}")
    lines.append("")
    lines.append("### 可用工具")
    lines.append("")
    lines.append("```")
    lines.append(result.tool_summary or "（无）")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


# =============================================================================
# 架构方案
# =============================================================================
# 测试助手：产出渲染
# =============================================================================
# 策划助手：产出渲染
# =============================================================================
# 安全狩猎：产出渲染
# =============================================================================

_CATEGORY_LABEL = {
    "authz": "越权 / 权限缺失",
    "race": "并发竞态",
    "boundary": "业务边界",
    "injection": "注入（逻辑型）",
    "crypto": "密码学误用",
    "consistency": "状态一致性",
}

#: 渲染顺序：越权和竞态排前面 —— 它们最常被利用、修复代价也最大。
_CATEGORY_ORDER = ["authz", "race", "boundary", "crypto", "consistency", "injection"]


def render_threat_model_markdown(result: AuditResult, *, repo: str = "", model: str = "") -> str:
    """威胁模型的 Markdown 渲染。

    每条都必须露出 **攻击路径** 与 **影响** —— 这两项缺任何一项，
    这份「威胁」就只是一句感觉，评审无法复核也无法排期。
    """
    threats = result.artifacts.get("threat", [])

    def get(obj, key, default=""):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, default)

    lines: list[str] = []
    lines.append("# 安全狩猎报告")
    lines.append("")
    lines.append(f"- **狩猎目标**：{result.goal}")
    if repo:
        lines.append(f"- **仓库路径**：`{repo}`")
    if model:
        lines.append(f"- **驱动模型**：`{model}`")
    lines.append(f"- **迭代轮数**：{result.iterations}")
    lines.append(f"- **威胁总数**：{len(threats)}")
    lines.append("")

    if not threats:
        lines.append("本次没有登记任何逻辑级威胁。")
        if result.stop_reason != "final_answer":
            lines.append(f"（结束原因：`{result.stop_reason}` —— 可能说明运行没有正常收尾。）")
        lines.append("")
        lines.append("> 注意：「没报」不等于「没问题」。狩猎依赖威胁模型 ——")
        lines.append("> 如果目标里没给出角色与权限关系，越权类问题很难被找到。")
        return "\n".join(lines)

    # ---- 按类别分组 ----
    by_cat: dict[str, list] = {}
    for t in threats:
        by_cat.setdefault(str(get(t, "category", "?")), []).append(t)

    lines.append("| 类别 | 数量 |")
    lines.append("|---|---|")
    for cat in _CATEGORY_ORDER:
        if cat in by_cat:
            lines.append(f"| {_CATEGORY_LABEL.get(cat, cat)} | {len(by_cat[cat])} |")
    for cat in by_cat:
        if cat not in _CATEGORY_ORDER:
            lines.append(f"| {cat} | {len(by_cat[cat])} |")
    lines.append("")

    # ---- 先给总表：让人一眼看出最该先处理什么 ----
    lines.append("## 威胁一览")
    lines.append("")
    lines.append("| 类别 | 威胁 | 位置 |")
    lines.append("|---|---|---|")
    ordered: list = []
    for cat in _CATEGORY_ORDER + [c for c in by_cat if c not in _CATEGORY_ORDER]:
        ordered.extend(by_cat.get(cat, []))
    for t in ordered:
        lines.append(
            f"| {_CATEGORY_LABEL.get(str(get(t, 'category')), str(get(t, 'category')))} "
            f"| {get(t, 'title')} "
            f"| `{get(t, 'file')}:{get(t, 'line')}` |"
        )
    lines.append("")

    # ---- 逐条 ----
    lines.append("## 详情")
    lines.append("")
    for t in ordered:
        cat = str(get(t, "category"))
        lines.append(f"### [{_CATEGORY_LABEL.get(cat, cat)}] {get(t, 'title')}")
        lines.append("")
        lines.append(f"- **位置**：`{get(t, 'file')}:{get(t, 'line')}`")
        actor = get(t, "actor")
        if actor:
            lines.append(f"- **谁能触发**：{actor}")
        # 攻击路径紧跟「谁能触发」：这两条合起来才是完整的可重放描述
        path = get(t, "attack_path")
        if path:
            lines.append(f"- **攻击路径**：{path}")
        else:
            lines.append("- **攻击路径**：（未给出 —— 这条还不可复核）")
        impact = get(t, "impact")
        if impact:
            lines.append(f"- **影响**：{impact}")
        pre = get(t, "preconditions")
        if pre:
            lines.append(f"- **触发前提**：{pre}")
        variants = get(t, "variants", []) or []
        if variants:
            lines.append(f"- **同类位置**：{'、'.join(f'`{v}`' for v in variants)}")
        ev = get(t, "evidence")
        if ev:
            lines.append(f"- **依据**：`{ev}`")
        overlap = get(t, "overlaps_rule")
        if overlap:
            lines.append(f"- **注**：与规则 `{overlap}` 重叠，可考虑合并")
        lines.append("")

    if result.warnings:
        lines.append("## 运行提示")
        lines.append("")
        for w in result.warnings:
            lines.append(f"- {w}")
        lines.append("")

    return "\n".join(lines)


def render_threat_model_terminal(result: AuditResult, *, color: bool = True) -> str:
    """终端版：类别分组，每条的「路径 → 影响」压成两行。"""

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    threats = result.artifacts.get("threat", [])

    def get(obj, key, default=""):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, default)

    lines: list[str] = []
    lines.append(paint(f"安全狩猎 · {len(threats)} 条威胁", _BOLD))
    lines.append(f"目标：{result.goal}")
    if not threats:
        lines.append("本次没有登记任何逻辑级威胁。")
        return "\n".join(lines)

    by_cat: dict[str, list] = {}
    for t in threats:
        by_cat.setdefault(str(get(t, "category", "?")), []).append(t)

    for cat in _CATEGORY_ORDER + [c for c in by_cat if c not in _CATEGORY_ORDER]:
        if cat not in by_cat:
            continue
        lines.append("")
        lines.append(paint(
            f"{_CATEGORY_LABEL.get(cat, cat)}（{len(by_cat[cat])} 条）", _BOLD))
        for t in by_cat[cat]:
            loc = f"{get(t, 'file')}:{get(t, 'line')}"
            lines.append(f"  {get(t, 'title')}  ({loc})")
            actor = get(t, "actor")
            path = get(t, "attack_path")
            if path:
                prefix = f"[{actor}] " if actor else ""
                lines.append(f"    路径：{prefix}{path}")
            lines.append(f"    影响：{get(t, 'impact') or '（未给出）'}")
    return "\n".join(lines)


# =============================================================================


def render_roadmap_markdown(result: AuditResult, *, repo: str = "", model: str = "") -> str:
    """实施计划的 Markdown 渲染。

    顺序刻意是「前提 → 步骤」：读计划的人先要知道「这些步骤建立在什么假设上」，
    否则会照着做，做到一半才发现前提不成立。

    每一步必须露出 **完成条件**（done_when）和**验证手段**（verify）——
    没有这两项，一份计划就退化成「建议按顺序做这几件事」，无法验收。
    """
    steps = sorted(result.artifacts.get("plan_step", []), key=lambda x: getattr(x, "order", 0))
    risks = result.artifacts.get("plan_risk", [])

    def get(obj, key, default=""):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, default)

    lines: list[str] = []
    lines.append("# 实施计划")
    lines.append("")
    lines.append(f"- **改动意图**：{result.goal}")
    if repo:
        lines.append(f"- **仓库路径**：`{repo}`")
    if model:
        lines.append(f"- **驱动模型**：`{model}`")
    lines.append(f"- **步骤数**：{len(steps)}")
    blocking = sum(1 for r in risks if get(r, "blocking", False))
    lines.append(f"- **前提/风险**：{len(risks)} 条（其中 {blocking} 条会阻塞）")
    lines.append("")

    if not steps:
        lines.append("本次没有登记任何计划步骤。")
        if result.stop_reason != "final_answer":
            lines.append(f"（结束原因：`{result.stop_reason}` —— 可能说明运行没有正常收尾。）")
        return "\n".join(lines)

    # ---- 前提放前面：读到步骤之前先知道它们建立在什么假设上 ----
    if risks:
        lines.append("## 前提与风险")
        lines.append("")
        for risk in risks:
            flag = "**[阻塞]** " if get(risk, "blocking", False) else ""
            lines.append(f"- {flag}{get(risk, 'question', '')}")
            why = get(risk, "why_it_matters", "")
            if why:
                lines.append(f"  - 不成立时：{why}")
        lines.append("")
    else:
        lines.append("> 这份计划**没有登记任何前提**。零前提通常说明作者还没真正评估过不确定性")
        lines.append("> —— 或者确实没有不确定的地方。两种都要让人知道。")
        lines.append("")

    # ---- 步骤 ----
    lines.append("## 步骤")
    lines.append("")
    for step in steps:
        order = get(step, "order", "?")
        lines.append(f"### {order}. {get(step, 'title', '')}")
        lines.append("")
        touches = get(step, "touches", []) or []
        if touches:
            lines.append(f"- **改哪里**：{'、'.join(f'`{t}`' for t in touches)}")
        why = get(step, "why", "")
        if why:
            lines.append(f"- **为什么需要**：{why}")
        done = get(step, "done_when", "")
        if done:
            # 完成条件紧跟「改哪里」，因为它是这一步的可验收性所在
            lines.append(f"- **做完是什么样**：{done}")
        verify = get(step, "verify", "")
        if verify:
            lines.append(f"- **怎么验证**：`{verify}`")
        else:
            lines.append("- **怎么验证**：（未给出 —— 这一步做完后无法自动确认）")
        invalidate = get(step, "may_invalidate", "")
        if invalidate:
            lines.append(f"- **可能推翻**：{invalidate}")
        evidence = get(step, "evidence", "")
        if evidence:
            lines.append(f"- **依据**：`{evidence}`")
        lines.append("")

    if result.warnings:
        lines.append("## 运行提示")
        lines.append("")
        for warn in result.warnings:
            lines.append(f"- {warn}")
        lines.append("")

    return "\n".join(lines)


def render_roadmap_terminal(result: AuditResult, *, color: bool = True) -> str:
    """终端版：步骤一行一条，突出完成条件。"""

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    steps = sorted(result.artifacts.get("plan_step", []), key=lambda x: getattr(x, "order", 0))
    risks = result.artifacts.get("plan_risk", [])

    def get(obj, key, default=""):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, default)

    lines: list[str] = []
    lines.append(paint(f"实施计划 · {len(steps)} 步 · {len(risks)} 条前提", _BOLD))
    lines.append(f"意图：{result.goal}")
    if not steps:
        lines.append("本次没有登记任何计划步骤。")
        return "\n".join(lines)

    blocking = [r for r in risks if get(r, "blocking", False)]
    if blocking:
        lines.append("")
        lines.append(paint(f"阻塞性前提（{len(blocking)} 条，未定就不该开始）：", _BOLD))
        for r in blocking:
            lines.append(f"  - {get(r, 'question', '')}")

    lines.append("")
    for step in steps:
        lines.append(paint(f"{get(step, 'order', '?')}. {get(step, 'title', '')}", _BOLD))
        touches = get(step, "touches", []) or []
        if touches:
            lines.append(f"   改：{'、'.join(touches)}")
        done = get(step, "done_when", "")
        if done:
            lines.append(f"   完成条件：{done}")
        verify = get(step, "verify", "")
        lines.append(f"   验证：{verify if verify else '（未给出）'}")
    return "\n".join(lines)


# =============================================================================

#: kind 的中文标签。放在这里而不是 prompt 里，因为渲染与 prompt 要一致，
#: 两处各写一份必然会漂。
_KIND_LABEL = {
    "characterization": "特征测试",
    "unit": "单元测试",
    "integration": "集成测试",
    "regression": "回归测试",
}


def render_test_plan_markdown(result: AuditResult, *, repo: str = "", model: str = "") -> str:
    """测试用例清单的 Markdown 渲染。

    顺序：按 kind 分组（特征测试在前）。理由与架构报告一致 ——
    读的人先看「这套用例是干什么的」，再看具体条目。

    **每条都必须露出 `should_fail_when`**：没有它，一份测试清单和
    「建议补测试」的空话没有区别。这个字段是这份产出的可评审性所在。
    """
    cases = result.artifacts.get("test_case", [])
    lines: list[str] = []
    lines.append("# 测试补全报告")
    lines.append("")
    lines.append(f"- **目标**：{result.goal}")
    if repo:
        lines.append(f"- **仓库路径**：`{repo}`")
    if model:
        lines.append(f"- **驱动模型**：`{model}`")
    lines.append(f"- **迭代轮数**：{result.iterations}")
    lines.append(f"- **用例总数**：{len(cases)}")
    # 验证状态必须单独统计并展示 ——「42 条，0 条跑过」和「42 条全跑通」
    # 是两件事，报告读起来必须能分辨
    _vcount = {"verified": 0, "failed": 0, "unverified": 0}
    for c in cases:
        key = (getattr(c, "verified", "") if not isinstance(c, dict) else c.get("verified", "")) or "unverified"
        _vcount[key] = _vcount.get(key, 0) + 1
    lines.append(
        f"- **验证状态**：跑通 {_vcount['verified']} / 跑过但失败 {_vcount['failed']}"
        f" / 未验证 {_vcount['unverified']}"
    )
    if _vcount["unverified"] and _vcount["unverified"] == len(cases):
        lines.append("")
        lines.append("> **注意：这批用例一条都没有被真实执行过。**"
                     "它们是按源码逐行推出来的规格说明，落地前需要先跑一遍再采信。")
    lines.append("")

    if not cases:
        lines.append("本次没有登记任何测试用例。")
        if result.stop_reason != "final_answer":
            lines.append(f"（结束原因：`{result.stop_reason}` —— 这可能说明运行没有正常收尾。）")
        return "\n".join(lines)

    # ---- 分组统计 ----
    by_kind: dict[str, list[Any]] = {}
    for case in cases:
        kind = str(getattr(case, "kind", "") or "unknown")
        by_kind.setdefault(kind, []).append(case)

    order = ["characterization", "unit", "integration", "regression"]
    kinds = [k for k in order if k in by_kind] + [k for k in by_kind if k not in order]

    lines.append("| 类型 | 数量 |")
    lines.append("|---|---|")
    for kind in kinds:
        lines.append(f"| {_KIND_LABEL.get(kind, kind)} | {len(by_kind[kind])} |")
    lines.append("")

    # ---- 特征测试单独说明 ----
    if "characterization" in by_kind:
        lines.append("> **关于特征测试**：它锁定的是代码**当前实际**的行为，包括那些看起来是错的。")
        lines.append("> 它建立的是一张**安全网**（改动会立刻显形），不是**正确性认证**。")
        lines.append("> 如果某条用例锁住的是一个已知缺陷，它会在修好之后变红 —— 那是预期的。")
        lines.append("")

    # ---- 逐条 ----
    for kind in kinds:
        lines.append(f"## {_KIND_LABEL.get(kind, kind)}（{len(by_kind[kind])} 条）")
        lines.append("")
        for case in by_kind[kind]:
            get = (lambda k, d="": getattr(case, k, d) or d) if not isinstance(case, dict) else (lambda k, d="": case.get(k) or d)
            title = get("title")
            target = get("target")
            file = get("file")
            line = get("line")
            lines.append(f"### {title}")
            lines.append("")
            lines.append(f"- **被测对象**：`{target}`　`{file}:{line}`")
            # 这个字段放第二位 —— 它是整条用例的证据强度所在
            lines.append(f"- **什么情况下它会红**：{get('should_fail_when')}")
            pinned = get("pins_behavior")
            if pinned:
                lines.append(f"- **锁定的当前行为**：{pinned}")
            rationale = get("rationale")
            if rationale:
                lines.append(f"- **为什么需要它**：{rationale}")
            evidence = get("evidence")
            if evidence:
                lines.append(f"- **依据**：`{evidence}`")
            status = get("verified", "unverified")
            label = {"verified": "已跑通", "failed": "跑过但失败"}.get(status, "未验证")
            lines.append(f"- **验证状态**：{label}")
            vout = get("verify_output")
            if vout:
                lines.append(f"- **执行输出**：`{str(vout)[:200]}`")
            code = get("code")
            if code:
                lines.append("")
                lines.append("```python")
                lines.append(str(code).strip())
                lines.append("```")
            else:
                lines.append("")
                lines.append("> 这条用例只有描述、没有可运行代码 —— 它是一份规格说明，不是能跑的测试。")
            lines.append("")

    if result.warnings:
        lines.append("## 运行提示")
        lines.append("")
        for warn in result.warnings:
            lines.append(f"- {warn}")
        lines.append("")

    return "\n".join(lines)


def render_test_plan_terminal(result: AuditResult, *, color: bool = True) -> str:
    """终端版：只给摘要 + 每条一行，代码不进终端（太长且没格式化）。"""

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    cases = result.artifacts.get("test_case", [])
    lines: list[str] = []
    lines.append(paint(f"测试补全 · 共 {len(cases)} 条用例", _BOLD))
    lines.append(f"目标：{result.goal}")
    if not cases:
        lines.append("本次没有登记任何测试用例。")
        return "\n".join(lines)

    by_kind: dict[str, list[Any]] = {}
    for case in cases:
        kind = str(getattr(case, "kind", "") or "unknown")
        by_kind.setdefault(kind, []).append(case)
    order = ["characterization", "unit", "integration", "regression"]
    kinds = [k for k in order if k in by_kind] + [k for k in by_kind if k not in order]

    lines.append("")
    for kind in kinds:
        lines.append(paint(f"{_KIND_LABEL.get(kind, kind)}（{len(by_kind[kind])} 条）", _BOLD))
        for case in by_kind[kind]:
            target = getattr(case, "target", "?") if not isinstance(case, dict) else case.get("target", "?")
            file = getattr(case, "file", "?") if not isinstance(case, dict) else case.get("file", "?")
            title = getattr(case, "title", "?") if not isinstance(case, dict) else case.get("title", "?")
            fail_when = getattr(case, "should_fail_when", "") if not isinstance(case, dict) else case.get("should_fail_when", "")
            status = getattr(case, "verified", "unverified") if not isinstance(case, dict) else case.get("verified", "unverified")
            label = {"verified": "✓已跑通", "failed": "✗跑过但失败"}.get(status, "未验证")
            lines.append(f"  [{label}] {title}")
            lines.append(f"    {target}  ({file})")
            lines.append(f"    会红于：{fail_when}")
        lines.append("")
    return "\n".join(lines)


# =============================================================================


def _meta_lines(result: AuditResult, *, repo: str, model: str, task: str) -> list[str]:
    lines = [f"- **{task}**：{result.goal}"]
    if repo:
        lines.append(f"- **仓库路径**：`{repo}`")
    if model:
        lines.append(f"- **驱动模型**：`{model}`")
    lines.append(f"- **迭代轮数**：{result.iterations}")
    lines.append(f"- **结束原因**：`{result.stop_reason}`")
    lines.append(f"- **耗时**：{result.elapsed:.1f}s")
    lines.append(
        f"- **Token**：prompt {result.usage.prompt_tokens} / completion "
        f"{result.usage.completion_tokens} / 缓存命中 {result.usage.cached_tokens}"
    )
    lines.append("")
    return lines


def render_blueprint_markdown(result: AuditResult, *, repo: str = "", model: str = "") -> str:
    """架构方案的 Markdown 渲染。

    顺序刻意是「层 → 组件 → 决策 → 开放问题」：读的人先看骨架，
    再看每个部件，最后看取舍与未定的事。反过来读会先被细节淹没。
    """
    layers = result.artifacts.get("layer", [])
    components = result.artifacts.get("component", [])
    decisions = result.artifacts.get("decision", [])
    questions = result.artifacts.get("open_question", [])

    lines: list[str] = ["# 架构设计报告", ""]
    lines.extend(_meta_lines(result, repo=repo, model=model, task="设计任务"))

    lines.append("## 结论")
    lines.append("")
    lines.append(result.summary.strip() or "（本次未给出文字结论）")
    lines.append("")

    # ---- 分层 ---- #
    lines.append(f"## 分层（{len(layers)}）")
    lines.append("")
    if not layers:
        lines.append("本次没有登记分层。")
        lines.append("")
    else:
        ordered_layers = sorted(layers, key=lambda item: getattr(item, "order", 0))
        for index, layer in enumerate(ordered_layers, start=1):
            lines.append(f"{index}. **{layer.name}** — {layer.purpose or '（未说明职责）'}")
            if layer.hint:
                lines.append(f"   - 边界：{layer.hint}")
        lines.append("")

    # ---- 组件：按层分组，与分层图的读法一致 ---- #
    lines.append(f"## 组件（{len(components)}）")
    lines.append("")
    if not components:
        lines.append("本次没有登记组件。")
        lines.append("")
    else:
        by_layer: dict[str, list[Any]] = {}
        for component in components:
            by_layer.setdefault(component.layer or "未分层", []).append(component)
        for layer_name, group in by_layer.items():
            lines.append(f"### {layer_name}")
            lines.append("")
            for component in group:
                title = component.name
                if component.role:
                    title += f"（{component.role}）"
                lines.append(f"- **{title}**")
                if component.responsibility:
                    lines.append(f"  - 职责：{component.responsibility}")
                if component.interfaces:
                    lines.append(f"  - 接口：{'、'.join(component.interfaces)}")
                if component.depends_on:
                    lines.append(f"  - 依赖：{'、'.join(component.depends_on)}")
                if component.failure_modes:
                    for mode in component.failure_modes:
                        lines.append(f"  - 失效：{mode}")
                if component.rationale:
                    lines.append(f"  - 为什么这样切：{component.rationale}")
            lines.append("")

    # ---- 决策 ---- #
    lines.append(f"## 决策（{len(decisions)}）")
    lines.append("")
    if not decisions:
        lines.append("本次没有登记决策。")
        lines.append("")
    else:
        for index, decision in enumerate(decisions, start=1):
            lines.append(f"### {index}. {decision.title}")
            lines.append("")
            if decision.decision:
                lines.append(f"- **选择**：{decision.decision}")
            if decision.rationale:
                lines.append(f"- **理由**：{decision.rationale}")
            if decision.alternatives:
                lines.append("- **放弃的方案**：")
                for alt in decision.alternatives:
                    lines.append(f"  - {alt}")
            if decision.consequences:
                lines.append(f"- **代价**：{decision.consequences}")
            lines.append("")

    # ---- 开放问题 ---- #
    blocking = [item for item in questions if item.blocking]
    lines.append(f"## 开放问题（{len(questions)}，其中 {len(blocking)} 项阻塞）")
    lines.append("")
    if not questions:
        lines.append("本次没有登记开放问题。")
        lines.append("")
        lines.append("> 一次架构设计若无任何未定事项，通常说明不确定被抹平了，而不是真的都定了。")
        lines.append("")
    else:
        ordered_questions = sorted(questions, key=lambda item: not item.blocking)
        for item in ordered_questions:
            mark = "**[阻塞]**" if item.blocking else "[待确认]"
            lines.append(f"- {mark} {item.question}")
            if item.why_it_matters:
                lines.append(f"  - 影响：{item.why_it_matters}")
        lines.append("")

    _append_warnings(lines, result)
    return "\n".join(lines)


def _append_warnings(lines: list[str], result: AuditResult) -> None:
    if not result.warnings:
        return
    lines.append("## 运行提示")
    lines.append("")
    for warning in result.warnings:
        for line in warning.splitlines():
            lines.append(f"- {line}")
    lines.append("")


# =============================================================================
# 终端
# =============================================================================


_TERMINAL_RENDERERS: dict[str, Callable[..., str]] = {
    "blueprint": lambda result, **kw: render_blueprint_terminal(result, **kw),
    "test_plan": lambda result, **kw: render_test_plan_terminal(result, **kw),
    "roadmap": lambda result, **kw: render_roadmap_terminal(result, **kw),
    "threat_model": lambda result, **kw: render_threat_model_terminal(result, **kw),
}


def render_terminal(result: AuditResult, *, color: bool = True) -> str:
    """按产出形态分派渲染器；未知形态走 generic，不冒充实出。"""
    renderer = _TERMINAL_RENDERERS.get(result.outcome)
    if renderer is not None:
        return renderer(result, color=color)
    if result.outcome != "findings" and "finding" not in result.artifacts:
        return render_generic_terminal(result, color=color)

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    counts = result.severity_counts
    lines: list[str] = []
    ordered = sort_findings(result.findings)

    lines.append("")
    lines.append(paint("─" * 64, _DIM))
    lines.append(paint("  审计完成", _BOLD))
    lines.append(paint("─" * 64, _DIM))
    lines.append(
        f"  轮数 {result.iterations}   耗时 {result.elapsed:.1f}s   结束原因 {result.stop_reason}"
    )
    lines.append(
        f"  token: prompt {result.usage.prompt_tokens} / completion "
        f"{result.usage.completion_tokens} / 缓存命中 {result.usage.cached_tokens}"
    )
    lines.append(
        "  发现: "
        + paint(f"阻断 {counts['blocker']}", _RED)
        + " / "
        + paint(f"警告 {counts['warn']}", _YELLOW)
        + " / "
        + paint(f"建议 {counts['note']}", _BLUE)
    )

    if ordered:
        lines.append("")
        for index, finding in enumerate(ordered, start=1):
            color = _SEVERITY_COLOR.get(finding.severity, "")
            label = SEVERITY_LABEL.get(finding.severity, finding.severity)
            lines.append(
                f"  {index}. {paint('[' + label + ']', color)} {finding.title}"
            )
            lines.append(paint(f"     {finding.file}:{finding.line}  {finding.id}", _DIM))
    else:
        lines.append("")
        lines.append(paint("  （没有记录发现；请结合运行提示判断覆盖是否充分）", _DIM))

    if result.summary:
        lines.append("")
        lines.append(paint("  结论：", _BOLD))
        for line in result.summary.strip().splitlines():
            lines.append(f"    {line}")

    if result.warnings:
        lines.append("")
        lines.append(paint("  运行提示：", _YELLOW))
        for warning in result.warnings:
            for line in warning.splitlines():
                lines.append(f"    {line}")

    lines.append("")
    return "\n".join(lines)


def render_blueprint_terminal(result: AuditResult, *, color: bool = True) -> str:
    """架构方案的终端摘要。与审查那份一样，只报代码算得准的计数。"""

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    layers = result.artifacts.get("layer", [])
    components = result.artifacts.get("component", [])
    decisions = result.artifacts.get("decision", [])
    questions = [item for item in result.artifacts.get("open_question", [])]
    blocking = sum(1 for item in questions if item.blocking)

    lines: list[str] = ["", paint("─" * 64, _DIM), paint("  设计完成", _BOLD), paint("─" * 64, _DIM)]
    lines.append(
        f"  轮数 {result.iterations}   耗时 {result.elapsed:.1f}s   结束原因 {result.stop_reason}"
    )
    lines.append(
        f"  token: prompt {result.usage.prompt_tokens} / completion "
        f"{result.usage.completion_tokens} / 缓存命中 {result.usage.cached_tokens}"
    )
    lines.append(
        f"  产出: 分层 {len(layers)} / 组件 {len(components)} / 决策 {len(decisions)}"
        + (paint(f" / 开放问题 {len(questions)}（阻塞 {blocking}）", _YELLOW) if questions else "")
    )

    if components:
        lines.append("")
        for component in components:
            deps = "、".join(component.depends_on) or "无"
            lines.append(f"  - {component.name}（{component.layer or '未分层'}）→ 依赖 {deps}")

    if result.summary:
        lines.append("")
        lines.append(paint("  结论：", _BOLD))
        for line in result.summary.strip().splitlines():
            lines.append(f"    {line}")

    if result.warnings:
        lines.append("")
        lines.append(paint("  运行提示：", _YELLOW))
        for warning in result.warnings:
            for line in warning.splitlines():
                lines.append(f"    {line}")

    lines.append("")
    return "\n".join(lines)


__all__ = [
    "render_blueprint_markdown",
    "render_blueprint_terminal",
    "render_markdown",
    "render_terminal",
    "sort_findings",
]
