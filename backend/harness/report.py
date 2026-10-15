"""报告渲染：Markdown 与终端两种形态。

报告只陈述**有证据的结论**；运行过程中的告警（护栏拒绝、核验失败、迭代耗尽）
会单列出来，避免把它们混进「发现」里冒充审计结论。
"""

from __future__ import annotations

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


def render_markdown(result: AuditResult, *, repo: str = "", model: str = "") -> str:
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
# 终端
# =============================================================================


def render_terminal(result: AuditResult, *, color: bool = True) -> str:
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


def render_findings_oneline(findings: list[Finding]) -> str:
    """给评估脚本用的紧凑表示：`severity file:line`。"""
    return "; ".join(
        f"{item.severity} {item.file}:{item.line}" for item in sort_findings(findings)
    ) or "(none)"


__all__ = ["render_markdown", "render_terminal", "render_findings_oneline", "sort_findings"]
