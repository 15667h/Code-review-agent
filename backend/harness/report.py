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
