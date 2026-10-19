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

from .agents import AgentSpec, UnknownAgent, default_id, get as get_agent
from .budget import Budget
from .config import Config
from .context import (
    STATUS_OPEN,
    ContextBuilder,
    compress_trajectory,
    goal_message,
    loaded_tools_message,
    status_message,
    validate_protocol,
)
from .guardrails import (
    is_circuit_exempt,
    ApprovalFn,
    Constrainer,
    Corrector,
    Verifier,
    check_completion_claims,
    verify_citations,
    verify_finding_locations,
)
from .arch_check import check_blueprint_quality, summary_lines, verify_blueprint
from .arch_check import summary_lines, verify_blueprint
from .judge import judge_findings
from .pacing import pacing_note
from .model import Model, ModelError
from .sandbox import CommandRunner, Workspace
from .tools import Risk, tool_report
from .types import Finding, Message, RunState, Usage

EventHook = Callable[[str, dict[str, Any]], None]
StopHook = Callable[[], bool]


@dataclass
class AgentResult:
    """一次运行的产出。

    ``artifacts`` 按类型分桶：审查只有 finding，架构有 layer / component /
    decision / open_question。``findings`` 保留为 finding 桶的视图，
    让报告、护栏、评估这些既有代码不必改。
    """

    goal: str
    agent: str = field(default_factory=default_id)
    artifacts: dict[str, list[Any]] = field(default_factory=dict)
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
    # 产出形态标识，与 AgentSpec.outcome 一致；前端据此选渲染器
    outcome: str = ""
    # 架构产出的证据核验报告（审查不用）。独立字段而不进 artifacts：
    # 产出桶里只该有 agent 声明的那几种类型，否则「按声明渲染」的约定会被打破。
    evidence_report: dict = field(default_factory=dict)

    @property
    def findings(self) -> list[Finding]:
        # 只读，理由与 RunState.findings 相同：读一下不该凭空建桶
        return self.artifacts.get("finding", [])

    @property
    def artifact_counts(self) -> dict[str, int]:
        return {kind: len(items) for kind, items in self.artifacts.items()}

    @property
    def severity_counts(self) -> dict[str, int]:
        counts = {"blocker": 0, "warn": 0, "note": 0}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts


# 旧名字保留：既有代码与测试都用 AuditResult
AuditResult = AgentResult


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


def _enforce_read_only(spec: AgentSpec, registry: ToolRegistry) -> None:
    """把 ``can_edit=False`` 从一句声明变成运行前的硬检查。

    没有这一步时，声明的边界与真实能力是脱钩的：架构助手说着「只读」却拿着
    ``run_command``（HIGH 风险，白名单命令能代写文件、git checkout、跑脚本），
    而用户打开高风险开关后执行层并不拦它。平台化的意义就在于**加一个 agent
    不改既有代码也不会削弱边界** —— 违规时直接失败，好过静默放行。
    """
    if spec.can_edit:
        return
    offenders = [
        tool.name
        for tool in registry.all()
        if tool.risk >= Risk.MEDIUM
    ]
    if offenders:
        raise RuntimeError(
            f"agent `{spec.id}` 声明了 can_edit=False，但工具箱里有 "
            f"{len(offenders)} 个中高风险工具：{', '.join(sorted(offenders))}。"
            "请从 build_tools 中移除它们，或把声明改为 can_edit=True。"
        )


def run_agent(
    *,
    goal: str,
    workspace: Workspace,
    model: Model,
    config: Config,
    agent: str | None = None,
    context_note: str = "",
    on_event: EventHook | None = None,
    approve: ApprovalFn | None = None,
    allow_high_risk: bool = False,
    max_model_retries: int = 2,
    should_stop: Callable[[], bool] | None = None,
    judge: bool = True,
) -> AgentResult:
    """跑完一次完整运行。

    ``agent`` 决定用哪套提示词、工具集与产出类型（见 ``agents.AgentSpec``）；
    不传则用默认 agent。未知 id 抛 :class:`UnknownAgent` —— 调用方要在入口校验，
    而不是让它在跑完之后才发现答非所问。

    ``should_stop`` 是给交互式前端用的外部中断开关：每轮开始时询问一次，
    返回 True 就停在这一轮并将 ``stop_reason`` 置为 ``stopped``。
    刻意做成轮询式而不是抛异常 —— 中断要停在一个干净的边界上，
    让已经产出的内容与轨迹保持完整，而不是把上下文撕成一半。

    ``judge`` 控制是否在收尾前跑一遍独立复核（见 ``judge.py``）。
    关掉它只用于 A/B 对比「有/无过滤层的差距」，默认开启。
    """
    started = time.time()

    spec = get_agent(agent or default_id())
    state = RunState(goal=goal, max_iterations=config.max_iterations)
    # 预置产出桶，保证键顺序与 agent 声明一致（前端按这个顺序展示）
    state.artifacts = {kind: [] for kind in spec.artifact_kinds}
    runner = CommandRunner(
        workspace=workspace,
        allow=config.allow_commands,
        timeout=config.timeout,
    )
    registry = spec.build_tools(workspace, state, runner)
    _enforce_read_only(spec, registry)
    core_tools = registry.core_schemas()

    builder = ContextBuilder(
        system_prompt=spec.system_prompt,
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

    result = AgentResult(
        goal=goal,
        agent=spec.id,
        outcome=spec.outcome,
        prefix_trace=builder.prefix_trace(),
        tool_summary=tool_report(registry),
    )

    trajectory: list[Message] = [goal_message(goal, context_note, heading=spec.goal_heading)]
    status_note = ""

    _emit(on_event, "start", {"prefix": result.prefix_trace, "goal": goal})

    while state.iteration < state.max_iterations:
        # 外部中断：停在干净边界上，已产生的发现与轨迹保持完整
        if should_stop is not None and should_stop():
            result.stop_reason = "stopped"
            break

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
        extra = spec.status_lines(state) if spec.status_lines else None
        # 轮次预算与助手自己的状态行并列展示：模型要能一眼看出「还剩几轮」，
        # 否则会边读边画直到撞上限，留下一份没写完的产出。
        pace = pacing_note(state.iteration, state.max_iterations)
        lines = ([pace] if pace else []) + (list(extra) if extra else [])
        messages = builder.build(
            trajectory,
            status_message(state, note=status_note, extra_lines=lines or None),
        )

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
            # 产出边录边推：先快照各桶长度，再看新增了什么。
            # 用「长度变化」判断而不是「工具名是 record_*」——
            # 重复条目会被去重吞掉，那时不能凭空推一条并不存在的产出。
            before = {kind: len(items) for kind, items in state.artifacts.items()}

            if check.allowed:
                # 一次正常工具被放行 → 说明模型已经找到可行路径，连续被拒计数归零。
                # 豁免类工具（finish / record_finding / update_todo）不参与归零：
                # 它们只是收尾与落库，不该被用来「洗掉」之前被拒的记录。
                if not is_circuit_exempt(call.name):
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

            for kind, items in state.artifacts.items():
                for item in items[before.get(kind, 0) :]:
                    _emit(
                        on_event,
                        "artifact",
                        {"kind": kind, "artifact": item, "iteration": state.iteration},
                    )
                    if kind == "finding":
                        # 旧契约：审查产出的事件名保持不变，既有前端无需改动
                        _emit(on_event, "finding", {"finding": item, "iteration": state.iteration})

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
    result.artifacts = {kind: list(items) for kind, items in state.artifacts.items()}
    result.iterations = state.iteration
    result.usage = state.usage
    result.trajectory = trajectory
    result.elapsed = time.time() - started

    if result.summary:
        result.warnings.extend(check_completion_claims(state, result.summary))
        result.warnings.extend(verify_citations(result.summary, state))

    # ---- 收尾复核：位置真实性 + 独立判官 ----
    # 顺序是有讲究的：先做零成本的确定性核验（引用的行号与描述对不对得上），
    # 再让判官只处理「位置站得住」的那些。反过来先花钱判一遍、再发现位置就是错的，
    # 那一轮 token 就白花了。
    #
    # 为什么不让模型自己核查：业界实测最强 LLM 判断「看似专业但其实是错的评论」
    # 召回只有约 20%。能确定性判定的地方不该交给模型。
    finding_dicts = [
        item.to_dict() if hasattr(item, "to_dict") else dict(item)
        for item in (result.artifacts.get("finding") or [])
    ]
    if finding_dicts and judge:
        kept, report = judge_findings(finding_dicts, workspace.root, model)
        if report.ungrounded:
            result.warnings.append(
                f"{report.ungrounded} 条发现的位置与描述对不上，未进入复核。"
            )
        result.warnings.extend(report.warnings())
        if len(kept) != len(finding_dicts):
            # 写回产出桶：前端与其他消费方读的是 artifacts，不是 warnings
            dropped_ids = {d.id for d in report.dropped}
            result.artifacts["finding"] = [
                item
                for item in result.artifacts["finding"]
                if (item.id if hasattr(item, "id") else str(item.get("id"))) not in dropped_ids
            ]
    elif finding_dicts:
        result.warnings.extend(verify_finding_locations(finding_dicts, workspace.root))

    # ---- 架构产出的证据核验 ----
    # 与审查的判定方式不同：架构说「这个层/组件存在于这里」，判据是
    # 文件存在 + 行号在范围内 + 指向的是代码而非空行，不需要关键词族。
    #
    # 报告**不放进 artifacts**：产出桶里只该有 agent 声明的那几种类型
    # （有测试守着这条约定）。放在 AgentResult 的独立字段上，
    # 前端与评估脚本读 result.evidence_report 即可。
    if spec.outcome == "blueprint":
        report = verify_blueprint(result.artifacts, workspace.root)
        result.evidence_report = report
        result.warnings.extend(summary_lines(report))
        # 权衡真实性：判别「这是好决策吗」需要人，但判别「它有没有写代价、
        # 备选是不是稻草人」可以机械做。业界公认这是最难识别的失败模式。
        result.warnings.extend(check_blueprint_quality(result.artifacts))

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


def run_audit(**kwargs: Any) -> AgentResult:
    """审查助手的便捷入口。

    等价于 ``run_agent(agent="review", ...)``。保留它是因为 CLI、评估脚本与
    既有测试都按这个名字调用；新代码请直接用 :func:`run_agent`。
    """
    kwargs.setdefault("agent", "review")
    return run_agent(**kwargs)


# =============================================================================
# 便捷入口
# =============================================================================


def resolve_workspace(path: str | Path) -> Workspace:
    root = Path(path).expanduser().resolve()
    return Workspace(root=root)
