"""ReAct 主循环。

书中的模型控制循环骨架：

    observation = Environment.observe()
    trajectory  = [observation]
    while true:
        actions = Model(Harness.build_context(trajectory))
        if len(actions) == 0: break
        allowed = Harness.constrain(actions)
        observation = Environment.apply(allowed)
        if not Harness.verify(Environment):
            observation = Harness.correct(Environment)
        trajectory.append(allowed, observation)

「观察空间与动作空间」在这里就是：观察 = 工具返回的文本，动作 = 工具调用。
在模型固定时，扩展这两者往往比换模型更有效 —— 所以工具集与截断策略是核心资产。

关于消息顺序：assistant 消息（含 tool_calls）必须**先于**对应的 tool 结果入轨迹，
否则 API 会拒绝；同一次响应的多个 tool 结果顺序需与 tool_calls 保持一致。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .budget import Budget
from .config import Config
from .context import (
    STATUS_OPEN,
    ContextBuilder,
    SYSTEM_PROMPT,
    compress_trajectory,
    goal_message,
    loaded_tools_message,
    status_message,
    validate_protocol,
)
from .guardrails import (
    CIRCUIT_EXEMPT,
    ApprovalFn,
    Constrainer,
    Corrector,
    Verifier,
    check_completion_claims,
    verify_citations,
)
from .model import Model, ModelError
from .sandbox import CommandRunner, Workspace
from .tools_audit import build_toolbox, tool_report
from .types import Finding, Message, RunState, Usage

EventHook = Callable[[str, dict[str, Any]], None]


@dataclass
class AuditResult:
    goal: str
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    iterations: int = 0
    stop_reason: str = ""
    usage: Usage = field(default_factory=Usage)
    trajectory: list[Message] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    compression_events: list[dict] = field(default_factory=list)
    prefix_trace: dict = field(default_factory=dict)
    elapsed: float = 0.0
    tool_summary: str = ""

    @property
    def severity_counts(self) -> dict[str, int]:
        counts = {"blocker": 0, "warn": 0, "note": 0}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts


# =============================================================================
# 辅助
# =============================================================================


def _prune_old_status(trajectory: Sequence[Message]) -> list[Message]:
    """只保留最后一条状态栏，其余旧状态栏删掉。

    旧状态的信息已经被新状态覆盖，属于噪声；它们又位于轨迹末尾，
    正是压缩应该动的地方。前缀不受影响。
    """
    keep: list[Message] = []
    seen_latest = False
    for message in reversed(trajectory):
        if message.role == "user" and message.content.startswith(STATUS_OPEN):
            if seen_latest:
                continue
            seen_latest = True
        keep.append(message)
    keep.reverse()
    return keep


def _emit(hook: EventHook | None, event: str, payload: dict[str, Any]) -> None:
    if hook is not None:
        try:
            hook(event, payload)
        except Exception:  # noqa: BLE001 - 观察者不该影响主流程
            pass


# =============================================================================
# 主循环
# =============================================================================


def run_audit(
    *,
    goal: str,
    workspace: Workspace,
    model: Model,
    config: Config,
    context_note: str = "",
    on_event: EventHook | None = None,
    approve: ApprovalFn | None = None,
    allow_high_risk: bool = False,
    max_model_retries: int = 2,
) -> AuditResult:
    """跑完一次完整审计。"""
    started = time.time()

    state = RunState(goal=goal, max_iterations=config.max_iterations)
    runner = CommandRunner(
        workspace=workspace,
        allow=config.allow_commands,
        timeout=config.timeout,
    )
    toolbox = build_toolbox(workspace, state, runner)
    registry = toolbox.registry
    core_tools = registry.core_schemas()

    builder = ContextBuilder(
        system_prompt=SYSTEM_PROMPT,
        core_tools=core_tools,
        budget=Budget(config.context_window),
    )
    constrainer = Constrainer(
        registry=registry,
        repeat_limit=config.repeat_limit,
        approve=approve,
        allow_high_risk=allow_high_risk,
    )
    verifier = Verifier(workspace=workspace.root)
    corrector = Corrector()

    result = AuditResult(goal=goal, prefix_trace=builder.prefix_trace(), tool_summary=tool_report(registry))

    trajectory: list[Message] = [goal_message(goal, context_note)]
    status_note = ""

    _emit(on_event, "start", {"prefix": result.prefix_trace, "goal": goal})

    while state.iteration < state.max_iterations:
        state.iteration += 1

        # ---- 预算：临近上限先压缩，避免溢出硬失败 ----
        if builder.over_budget(trajectory):
            before = len(trajectory)
            trajectory = _prune_old_status(trajectory)
            trajectory, stats = compress_trajectory(trajectory)
            tokens = builder.prefix_tokens() + sum(
                len(message.content) // 2 for message in trajectory
            )
            event = {"iteration": state.iteration, "messages": before, "stats": stats, "approx_tokens": tokens}
            result.compression_events.append(event)
            _emit(on_event, "compression", event)

        # ---- 构造上下文并调用模型 ----
        messages = builder.build(trajectory, status_message(state, note=status_note))

        # 发出去之前先本地校验一遍消息协议。这一步是 Harness 的自我保护：
        # 编排错了就让错误停在本地并说清位置，而不是让 API 回一句「tool 消息数量不足」。
        problems = validate_protocol(messages)
        if problems:
            result.stop_reason = "protocol_error"
            result.warnings.append("上下文违反消息协议（Harness 编排错误）：\n" + "\n".join(problems))
            break

        decision = None
        last_error: ModelError | None = None

        for attempt in range(max_model_retries + 1):
            try:
                decision = model.complete(messages, tools=core_tools)
                break
            except ModelError as exc:
                last_error = exc
                if not exc.retryable or attempt == max_model_retries:
                    break
                backoff = min(2**attempt, 8)
                _emit(on_event, "model_retry", {"attempt": attempt + 1, "error": str(exc), "backoff": backoff})
                time.sleep(backoff)

        if decision is None:
            result.stop_reason = "model_error"
            result.warnings.append(
                f"模型调用失败：{last_error.error_type if last_error else 'Unknown'} - {last_error}"
                if last_error
                else "模型调用失败"
            )
            break

        state.usage = state.usage + decision.usage
        _emit(
            on_event,
            "decision",
            {"iteration": state.iteration, "decision": decision, "usage": decision.usage},
        )

        # ---- 没有工具调用：视为最终答复 ----
        if decision.is_final:
            result.summary = decision.content
            result.stop_reason = "final_answer"
            break

        # ---- 执行工具 ----
        trajectory.append(decision.to_message())
        status_note = ""
        # 动态加载的工具 schema 先攒着，等整批 tool 结果都入轨迹后再插入。
        #
        # 这里是踩过的真坑：原先在 discover_tools 的 tool 消息之后立刻 append 这条
        # user 消息。当模型在**同一次响应里**同时调用了 discover_tools 和别的工具时，
        # 这条 user 消息就夹在了两条 tool 结果之间，于是 assistant(tool_calls) 后面
        # 只跟了 1 条 tool 消息就被打断，API 直接 400：
        #   "insufficient tool messages following tool_calls message"
        # 工具并发调用是常态，所以这个顺序必须结构化地保证，而不是靠调用习惯。
        pending_tool_docs: list[str] = []

        for call in decision.tool_calls:
            state.note_tool(call.signature())
            check = constrainer.check(call, state)

            if check.allowed:
                # 一次正常工具被放行 → 说明模型已经找到可行路径，连续被拒计数归零。
                # 豁免类工具（finish / record_finding / update_todo）不参与归零：
                # 它们只是收尾与落库，不该被用来「洗掉」之前被拒的记录。
                if call.name not in CIRCUIT_EXEMPT:
                    state.reject_streak = 0
                tool_result = registry.execute(call)
            else:
                state.rejected_calls += 1
                state.reject_streak += 1
                tool_result = check.result

            note = corrector.observe(call, tool_result)
            if note:
                status_note = note

            verification = verifier.verify(call, tool_result, state)
            if not verification.ok:
                # 结构化的失败信号：文件没改动成功这类问题必须让模型知道
                tool_result = tool_result.__class__.failure(
                    "VerificationFailed",
                    "；".join(verification.notes) or "操作结果未通过核验。",
                    call.arguments,
                    suggestion="先确认目标状态，再决定是否重试；不要假定操作已生效。",
                )

            trajectory.append(
                Message(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.name,
                    content=tool_result.render(),
                )
            )

            if call.name == "discover_tools" and tool_result.ok:
                pending_tool_docs.append(str(tool_result.data))

            _emit(
                on_event,
                "tool",
                {"call": call, "result": tool_result, "verification": verification.notes},
            )

            if call.name == "finish" and tool_result.ok:
                result.summary = str(call.arguments.get("summary", ""))
                result.stop_reason = "finished"
                break

        # 整批 tool 结果已配对完毕，此时插入 user 消息是安全的。
        for payload_text in pending_tool_docs:
            trajectory.append(loaded_tools_message(payload_text))

        if result.stop_reason == "finished":
            break

    else:
        result.stop_reason = "max_iterations"

    if not result.stop_reason:
        result.stop_reason = "max_iterations"

    # ---- 收尾核验 ----
    result.findings = list(state.findings)
    result.iterations = state.iteration
    result.usage = state.usage
    result.trajectory = trajectory
    result.elapsed = time.time() - started

    if result.summary:
        result.warnings.extend(check_completion_claims(state, result.summary))
        result.warnings.extend(verify_citations(result.summary, state))

    if corrector.total_failures:
        result.warnings.append(
            f"运行中发生 {corrector.total_failures} 次工具失败：\n{corrector.summary()}"
        )
    if state.rejected_calls:
        result.warnings.append(f"有 {state.rejected_calls} 次工具调用被护栏拒绝。")
    if result.stop_reason == "max_iterations":
        result.warnings.append(
            f"达到迭代上限 {state.max_iterations} 轮仍未收尾，结论可能不完整。"
        )

    _emit(on_event, "done", {"result": result})
    return result


# =============================================================================
# 便捷入口
# =============================================================================


def resolve_workspace(path: str | Path) -> Workspace:
    root = Path(path).expanduser().resolve()
    return Workspace(root=root)
