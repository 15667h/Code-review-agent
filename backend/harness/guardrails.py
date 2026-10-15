"""护栏：约束 / 验证 / 纠正。

书中的关键判断：生产级 Harness 的绝大部分代码都在这三件事上，而不是在「给模型工具」上。
三条设计纪律在这里的具体体现：

- **约束**：故障安全默认值 —— 能力默认关闭，必须显式开放；高风险动作要额外审查。
- **验证**：只看**结构化**结果（工具返回、文件是否真的变了），不看模型自由生成的文本 ——
  后者可能已被提示注入操纵。
- **纠正**：连续失败要断电（熔断），不要把半成品当结果展示；拒绝理由要作为工具结果回传给模型，
  让它用自己的「工具失败」处理能力去消化，而不是简单重试同一次调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .tools import Risk, ToolRegistry
from .types import RunState, ToolCall, ToolResult

ApprovalFn = Callable[[ToolCall, Risk], tuple[bool, str]]

# 连续被拒熔断的豁免名单。
#
# 熔断的唯一目的是「别让模型把预算烧死在被禁止的操作上」，而不是切断它的全部能力。
# 这三类工具本身不改动外部环境（风险由参数校验与状态写入承担），一旦被熔断牵连：
#   - finish 被拒 → 循环无法正常收尾，只能撞到 max_iterations；
#   - record_finding / update_todo 被拒 → 已经查实的发现全部丢失。
# 也就是把一次「纪律问题」放大成「结果丢失」。真实运行中已经踩到过：
# 模型连试几次高风险工具后，14 条发现一条都没能落库。
CIRCUIT_EXEMPT = frozenset({"finish", "record_finding", "update_todo"})


# =============================================================================
# 约束
# =============================================================================


@dataclass
class ConstraintDecision:
    allowed: bool
    reason: str = ""
    result: ToolResult | None = None


@dataclass
class Constrainer:
    """工具调用前的准入检查。"""

    registry: ToolRegistry
    repeat_limit: int = 3
    approve: ApprovalFn | None = None
    # 故障安全默认值：未显式放开时，高风险工具一律拒绝
    allow_high_risk: bool = False
    reject_streak_limit: int = 5

    def check(self, call: ToolCall, state: RunState) -> ConstraintDecision:
        tool = self.registry.get(call.name)
        if tool is None:
            # 交由 registry.execute 产出标准的 UnknownTool 结果
            return ConstraintDecision(allowed=True)

        # ---- 重复调用熔断 ----
        # 这是逐工具、可自愈的：参数一改，state.repeat_streak 就归零。
        signature = call.signature()
        if signature == state.last_signature and state.repeat_streak >= self.repeat_limit:
            return ConstraintDecision(
                allowed=False,
                reason="repeat",
                result=ToolResult.failure(
                    "RepeatedCall",
                    f"`{call.name}` 已连续用完全相同的参数调用了 {state.repeat_streak} 次。",
                    call.arguments,
                    suggestion="重复同样的调用不会产生新信息。请改变参数、换一个工具，"
                    "或基于已有证据直接给出结论。",
                ),
            )

        # ---- 连续被拒熔断 ----
        # 判据是 state.reject_streak（连续），不是 rejected_calls（累计）。
        # 早期版本用累计值且从不归零，于是熔断一旦打开就再也关不上，连 finish 都被拒 ——
        # 模型被自己的护栏锁死。豁免名单见 CIRCUIT_EXEMPT。
        #
        # 注意一个边界：同一次响应里的多个并行调用会连续累加计数，
        # 所以一批 5 个高风险调用就能打开熔断。这是可接受的 ——
        # 打开后只拦非豁免工具，模型仍能记录发现并正常收尾。
        if call.name not in CIRCUIT_EXEMPT and state.reject_streak >= self.reject_streak_limit:
            return ConstraintDecision(
                allowed=False,
                reason="reject_streak",
                result=ToolResult.failure(
                    "CircuitOpen",
                    f"已连续 {state.reject_streak} 次调用被拒绝，该工具箱暂时熔断。",
                    call.arguments,
                    suggestion="停止尝试被禁止的操作。用只读工具继续调查，"
                    "或直接用 record_finding 记录已确认的结论、用 finish 收尾。",
                ),
            )

        # ---- 风险分级审批 ----
        if tool.risk is Risk.HIGH and not self.allow_high_risk:
            if self.approve is not None:
                ok, note = self.approve(call, tool.risk)
                if ok:
                    return ConstraintDecision(allowed=True, reason=note)
                return ConstraintDecision(
                    allowed=False,
                    reason="approval_denied",
                    result=ToolResult.failure(
                        "ApprovalDenied",
                        f"高风险工具 `{call.name}` 的审批未通过：{note or '未说明理由'}",
                        call.arguments,
                        suggestion="改用低风险工具，或说明必要性后改用其他路径。",
                    ),
                )
            return ConstraintDecision(
                allowed=False,
                reason="high_risk_blocked",
                result=ToolResult.failure(
                    "HighRiskBlocked",
                    f"`{call.name}` 属于高风险工具，当前运行未开启高风险权限。",
                    call.arguments,
                    suggestion="避免使用它。审计通常可以用只读工具完成；"
                    "若确实需要执行命令，请让使用者显式放开权限。",
                ),
            )

        return ConstraintDecision(allowed=True)


# =============================================================================
# 验证
# =============================================================================


@dataclass
class Verification:
    """一次工具调用之后的核验结论。"""

    ok: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class Verifier:
    """自动核验工具是否真的产生了预期效果。

    刻意只依赖结构化事实：文件是否真的改了、内容是否还能解析。
    不读模型的自由文本 —— 那是提示注入最容易藏身的地方。
    """

    workspace: Path | None = None

    def verify(self, call: ToolCall, result: ToolResult, state: RunState) -> Verification:
        notes: list[str] = []

        # 写操作：确认文件真的变了，并且仍是合法文本
        if call.name == "apply_patch" and result.ok:
            path = str(call.arguments.get("path", ""))
            if path and self.workspace is not None:
                target = (self.workspace / path).resolve()
                if not target.is_file():
                    return Verification(
                        ok=False,
                        notes=[f"补丁声称成功，但目标文件不存在：{path}"],
                    )
                try:
                    target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    return Verification(
                        ok=False,
                        notes=[f"补丁写入后文件无法按 utf-8 读回：{exc}"],
                    )
                notes.append(f"已确认 {path} 可读回（{target.stat().st_size} 字节）")

        # 读文件：确认返回的内容里确实有行号（说明不是空文件/错误路径）
        if call.name == "read_file" and result.ok:
            body = str(result.data or "")
            if "共 0 行" in body or body.strip().endswith("0 行"):
                notes.append("目标文件为空。如果不是预期情况，请确认路径是否正确。")

        # 记录被读过的文件，供状态栏展示
        if call.name in {"read_file", "read_symbols", "lint_file"} and result.ok:
            path = str(call.arguments.get("path", ""))
            if path:
                state.files_read.add(path)

        return Verification(ok=True, notes=notes)


# =============================================================================
# 纠正
# =============================================================================


@dataclass
class Corrector:
    """失败熔断与降级。"""

    # 连续工具失败多少次就停止尝试工具，强制让模型下结论
    failure_limit: int = 5
    consecutive_failures: int = 0
    total_failures: int = 0
    history: list[str] = field(default_factory=list)

    def observe(self, call: ToolCall, result: ToolResult) -> str:
        """记录一次结果，返回需要注入到状态栏的提示（无则空串）。"""
        if result.ok:
            self.consecutive_failures = 0
            return ""

        self.consecutive_failures += 1
        self.total_failures += 1
        line = f"{call.name}: {result.error_type} - {result.message[:120]}"
        self.history.append(line)

        if self.consecutive_failures >= self.failure_limit:
            return (
                f"已连续 {self.consecutive_failures} 次工具调用失败。"
                "请停止继续尝试工具：要么换一个明确不同的方案，要么直接调用 finish "
                "说明卡在哪里、已经确认了什么。"
            )
        if self.consecutive_failures >= 2:
            return (
                f"已有 {self.consecutive_failures} 次连续失败。"
                "先读一遍错误里的 [建议] 字段，确认参数或路径哪里不对，再动手。"
            )
        return ""

    def summary(self) -> str:
        if not self.history:
            return "（无工具失败）"
        return "\n".join(f"- {line}" for line in self.history[-8:])


# =============================================================================
# 完成度核验（对标书里的「做对了但说错了」）
# =============================================================================


def check_completion_claims(state: RunState, final_text: str) -> list[str]:
    """检查模型声称的完成情况与它实际做的事情是否一致。

    书中 τ²-bench 的数据：704 次失败运行里有 162 次是「信息告知出错」，
    其中 80 次环境状态其实是对的 —— 做对了但说错了。这里做最基础的粗筛。
    """
    problems: list[str] = []

    if not state.findings and "没有" not in final_text and "未发现" not in final_text:
        problems.append("发现数为 0，但总结里没有说明「检查了哪些范围」")

    if state.todos:
        unfinished = [todo for todo in state.todos if todo.status not in {"completed", "cancelled"}]
        if unfinished and "未完成" not in final_text and "未覆盖" not in final_text:
            problems.append(
                f"任务列表仍有 {len(unfinished)} 项未完成，但总结里没有提到未覆盖的部分"
            )

    if state.rejected_calls and "拒绝" not in final_text and "受限" not in final_text:
        problems.append(f"有 {state.rejected_calls} 次调用被护栏拒绝，总结里没有说明")

    return problems


def extract_cited_locations(text: str) -> set[str]:
    """从自由文本里抽取 `file:line` 形式的引用，用于粗略核对是否编造位置。"""
    import re

    return set(re.findall(r"[\w./\\-]+\.[A-Za-z0-9_]+:\d+", text))


def verify_citations(text: str, state: RunState) -> list[str]:
    """核对结论里引用的位置是否为本次实际读过的文件。

    只做保守判断：如果引用的文件从未出现在 files_read 里，就提示一次 ——
    可能是编造的行号，也可能是通过 grep 看到的（grep 也会记进 files_read）。
    """
    cited = extract_cited_locations(text)
    if not cited:
        return []

    known = {path for path in state.files_read}
    unknown: list[str] = []
    for item in cited:
        path = item.rsplit(":", 1)[0]
        normalized = path.replace("\\", "/").lstrip("./")
        if not any(normalized.endswith(known_path) or known_path.endswith(normalized) for known_path in known):
            unknown.append(item)
    if unknown:
        return [f"以下引用位置在本次运行中没有被读取过，需人工复核：{', '.join(sorted(unknown)[:8])}"]
    return []
