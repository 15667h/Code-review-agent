"""上下文构造与压缩。

书中的权威构造伪代码：

    request.messages = [stable_prefix] + trajectory + [status_message]
    request.tools    = stable_tools

三条铁律：
1. 系统提示词和工具定义一旦确定就不再改动 —— 改一个空格都会让缓存从该点起失效；
2. 动态信息永远追加到末尾（时间、进度、状态都放末尾，不改前缀）；
3. 使用标准 API 消息格式，不自行把 role/content 拼成纯文本。

压缩按书中「五层压缩」里适合本地运行的两层实现：
- L2 噪声删除：无信息量的短工具结果直接丢弃，**不做摘要**（对噪声做摘要只是浪费 token）；
- L4 归档压缩：超长旧工具结果替换为「头尾 + 关键行」摘要，并打上 ``[COMPRESSED]`` 标记防重复压缩。
全量 LLM 压缩留作接口，未默认启用 —— 它需要额外的模型调用，属于最后手段。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .budget import Budget, estimate_messages, estimate_tools
from .types import Finding, Message, RunState, Todo

# =============================================================================
# 上下文构造
# =============================================================================
#
# 系统提示词不在本文件。它属于 agent 自己的声明（见 ``agents.AgentSpec``），
# 因为「怎么想」和「能做什么」「产出什么」是一件事的三面，拆开就必然对不上。


@dataclass
class ContextBuilder:
    """构造每次模型调用的完整上下文。

    静态前缀只由两样东西组成：系统提示词 + 核心工具定义，**顺序固定、内容不变**。
    动态加载进来的工具 schema 不进前缀，而是作为轨迹里的消息存在。
    """

    system_prompt: str
    core_tools: list[dict]
    budget: Budget
    _prefix_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._prefix_fingerprint = self._fingerprint()

    # ---- 前缀 ---- #

    def prefix_tokens(self) -> int:
        return estimate_messages([Message(role="system", content=self.system_prompt)]) + estimate_tools(
            self.core_tools
        )

    def _fingerprint(self) -> str:
        payload = json.dumps(
            {"system": self.system_prompt, "tools": self.core_tools},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def prefix_fingerprint(self) -> str:
        """静态前缀指纹。运行期间它必须保持不变，一旦变化说明缓存会被击穿。"""
        return self._prefix_fingerprint

    def prefix_trace(self) -> dict:
        return {
            "fingerprint": self.prefix_fingerprint,
            "tokens": self.prefix_tokens(),
            "tool_count": len(self.core_tools),
        }

    # ---- 组装 ---- #

    def build(self, trajectory: Sequence[Message], status_message: Message | None = None) -> list[Message]:
        messages = [Message(role="system", content=self.system_prompt), *trajectory]
        if status_message is not None:
            messages.append(status_message)
        return messages

    def over_budget(self, trajectory: Sequence[Message]) -> bool:
        return self.budget.should_compress(self.prefix_tokens(), list(trajectory))


# =============================================================================
# 状态栏
# =============================================================================

STATUS_OPEN = "<agent_status>"
STATUS_CLOSE = "</agent_status>"

_KEY_LINE = re.compile(
    r"(error|exception|fail(ed|ure)?|traceback|timeout|panic|refused|denied|"
    r"错误|异常|失败|超时|拒绝|不存在|无法)",
    re.IGNORECASE,
)


def render_status(
    state: RunState,
    *,
    note: str = "",
    extra_lines: Sequence[str] | None = None,
) -> str:
    """把运行状态渲染成状态栏文本。

    书中的两条维护铁律：状态栏由**代码**维护，不让模型自己统计；
    并且它是对原始上下文的有损投影，只承载「代码算得准」的东西（计数、进度、时间）。

    产出部分按桶计数，不写死「findings」—— 架构助手产出的是 layer / component /
    decision / open_question，写成「findings: 0」会让它以为自己什么都没做。
    agent 想补充细节（如严重级别分布）通过 ``extra_lines`` 追加。
    """
    import datetime as _dt

    lines = [STATUS_OPEN, "Current State:"]

    done = sum(1 for todo in state.todos if todo.status == "completed")
    lines.append(f"- iteration: {state.iteration}/{state.max_iterations}")
    lines.append(f"- todo_done: {done}/{len(state.todos)}")

    counts = state.artifact_counts()
    if counts:
        lines.append("- artifacts: " + ", ".join(f"{kind}={n}" for kind, n in counts.items()))
    else:
        lines.append("- artifacts: (none)")

    for line in extra_lines or []:
        lines.append(line)

    if state.tool_counts:
        hottest = sorted(state.tool_counts.items(), key=lambda pair: pair[1], reverse=True)[:6]
        lines.append("- tool_calls: " + ", ".join(f"{name}={count}" for name, count in hottest))

    if state.repeat_streak >= 2 and state.last_signature:
        name = state.last_signature.split(":", 1)[0]
        lines.append(
            f"- WARNING: 连续 {state.repeat_streak} 次调用同一个 `{name}` 且参数完全相同；"
            "继续重复不会有新信息，请换参数、换工具或直接下结论。"
        )

    if state.files_read:
        recent = list(sorted(state.files_read))[-6:]
        lines.append(f"- files_read({len(state.files_read)}): " + ", ".join(recent))

    if state.todos:
        lines.append("- TODO:")
        for todo in state.todos:
            lines.append(f"    {todo.to_line()}")

    lines.append(f"- time: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if note:
        lines.append(f"- note: {note}")
    lines.append(STATUS_CLOSE)
    return "\n".join(lines)


def status_message(
    state: RunState,
    *,
    note: str = "",
    extra_lines: Sequence[str] | None = None,
) -> Message:
    """状态栏是一条 user 消息，固定放在轨迹最末尾。

    放末尾的原因：既不破坏前面的缓存前缀，又能获得最高的注意力权重。
    """
    return Message(role="user", content=render_status(state, note=note, extra_lines=extra_lines))


def goal_message(goal: str, context: str = "", heading: str = "审计目标") -> Message:
    """首条 user 消息：任务目标。属于轨迹，不属于静态前缀。

    heading 由 agent 声明（审查是「审计目标」，架构是「设计任务」）。
    """
    body = f"# {heading}\n\n{goal.strip()}"
    if context.strip():
        body += f"\n\n# 补充信息\n\n{context.strip()}"
    return Message(role="user", content=body)


# =============================================================================
# 压缩
# =============================================================================

COMPRESSED_TAG = "[COMPRESSED]"


def summarize_tool_output(text: str, *, head: int = 4, tail: int = 4, max_chars: int = 1500) -> str:
    """把一段工具输出压成「头 + 关键行 + 尾」。

    书中的四条压缩原则在这里的体现：保留决策依据（关键行），丢弃噪声（中间的重复输出），
    语义要完整（错误信息连上下文一起保留），并且明确标注这是压缩产物。
    """
    if len(text) <= max_chars:
        return text

    lines = text.splitlines()
    picked: list[str] = list(lines[:head])

    key_lines = [line for line in lines[head : len(lines) - tail] if _KEY_LINE.search(line)]
    if key_lines:
        picked.append("  …（关键行）")
        picked.extend(key_lines[:12])

    if len(lines) > head + tail:
        picked.append("  …")
        picked.extend(lines[-tail:])

    body = "\n".join(picked)
    if len(body) > max_chars:
        body = body[:max_chars]
    return f"{body}\n{COMPRESSED_TAG} 原文 {len(text)} 字符，已保留头尾与错误行"


def _is_noise(message: Message) -> bool:
    """噪声：几乎不携带信息的工具结果。直接丢弃，不做摘要。"""
    if message.role != "tool":
        return False
    stripped = message.content.strip()
    if not stripped:
        return True
    return len(stripped) < 24 and not _KEY_LINE.search(stripped)


def compress_trajectory(
    trajectory: Sequence[Message],
    *,
    keep_recent: int = 8,
    min_compress_chars: int = 2000,
) -> tuple[list[Message], dict]:
    """对轨迹做一次压缩。返回 ``(新轨迹, 统计)``。

    保护规则（对应书中 ``preserve=[decisions, constraints, failures, citations]``）：
    - system / user / assistant 消息一律保留（决策与约束）；
    - 含错误信息的工具结果即使很短也保留（失败证据）；
    - 只压缩「较旧的、较长的、且未被压缩过」的工具结果。
    """
    total = len(trajectory)
    frozen_from = max(0, total - keep_recent)
    result: list[Message] = []
    stats = {"dropped_noise": 0, "compressed": 0, "chars_before": 0, "chars_after": 0}

    for index, message in enumerate(trajectory):
        is_old = index < frozen_from

        if is_old and _is_noise(message):
            stats["dropped_noise"] += 1
            continue

        should_compress = (
            is_old
            and message.role == "tool"
            and not message.compressed
            and COMPRESSED_TAG not in message.content
            and len(message.content) > min_compress_chars
            and not _has_failure_signal(message)
        )

        if not should_compress:
            result.append(message)
            continue

        stats["chars_before"] += len(message.content)
        summary = summarize_tool_output(message.content)
        stats["chars_after"] += len(summary)
        stats["compressed"] += 1
        result.append(
            Message(
                role="tool",
                content=summary,
                tool_call_id=message.tool_call_id,
                name=message.name,
                compressed=True,
                raw_content=message.content,
            )
        )

    return result, stats


def _has_failure_signal(message: Message) -> bool:
    """失败证据要保留原样：错误细节被压缩掉，模型就失去了纠正的依据。"""
    head = message.content[:600]
    return bool(_KEY_LINE.search(head))


# =============================================================================
# 动态加载工具（渐进披露的落地形式）
# =============================================================================


def loaded_tools_message(payload: str) -> Message:
    """把 discover_tools 返回的工具 schema 追加到轨迹末尾。

    这是书中「工具定义动态加载」的做法：schema 作为轨迹的一部分固定在原位，
    静态前缀保持字节稳定，后续轮次不再搬运。

    **调用时机有硬约束**：必须等同一轮的所有 tool 结果都入轨迹之后再追加。
    若插在两条 tool 消息中间，API 会判定「assistant(tool_calls) 后的 tool 消息数量不足」
    并返回 400 —— 详见 ``validate_protocol`` 与 ``loop.run_audit`` 里的注释。
    """
    return Message(
        role="user",
        content=(
            "[tool_discovery] 以下工具的完整定义已加载，后续轮次可直接调用，"
            "无需再次检索：\n\n" + payload
        ),
    )


def validate_protocol(messages: Sequence[Message]) -> list[str]:
    """本地校验消息序列是否合法，返回问题描述（空列表 = 合法）。

    API 侧对 tool 配对是硬约束：一条含 ``tool_calls`` 的 assistant 消息，
    必须**紧接着**跟上一一对应的 tool 消息，中间不能插入任何其它角色的消息。
    本地先查一遍的收益是把一句难懂的 400 换成一条能直接定位的编排错误。
    """
    problems: list[str] = []
    index = 0
    total = len(messages)

    while index < total:
        message = messages[index]

        if message.role == "assistant" and message.tool_calls:
            expected = [call.id for call in message.tool_calls]
            seen: list[str] = []
            cursor = index + 1
            while cursor < total and messages[cursor].role == "tool":
                seen.append(messages[cursor].tool_call_id or "")
                cursor += 1

            missing = [item for item in expected if item not in seen]
            if missing:
                nxt = messages[cursor].role if cursor < total else "(轨迹结束)"
                problems.append(
                    f"assistant#{index} 声明了 {len(expected)} 个 tool_call，"
                    f"但只跟了 {len(seen)} 条 tool 结果（其后的角色是 {nxt}）；"
                    f"缺少 {missing}"
                )

            # 跳过这一整批 tool 结果，避免把它们当成孤立 tool 消息再次检查。
            # 注意必须前移 index，否则遇到带 tool_calls 的 assistant 就会原地打转。
            index = cursor
            continue

        if message.role == "tool":
            previous = messages[index - 1] if index > 0 else None
            if previous is not None and previous.role != "tool" and not previous.tool_calls:
                problems.append(
                    f"tool#{index} 之前是一条不含 tool_calls 的消息，无法配对。"
                )

        index += 1

    return problems


