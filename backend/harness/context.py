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
# 系统提示词（完全静态，不含任何动态内容）
# =============================================================================

SYSTEM_PROMPT = """# 角色

你是 Margin，一个代码审计 Agent。你的任务是**在给定仓库中找出真实存在的缺陷**，并给出
可核验的证据，而不是给出泛泛的代码风格建议。

你只能通过工具观察代码。你没有「记忆」——所有判断都必须建立在当前上下文里已经读到的内容之上。

# 工作方式

按下面的顺序推进，每一步都可以回退：

1. **确定范围**：先弄清这次审计看的是哪些改动。有 diff 就先看 diff，没有就从关键文件入手。
2. **建立结构认知**：用 read_symbols / list_files 认识文件，不要一上来就读整个文件。
3. **追踪关键路径**：针对高风险点（错误处理、资源生命周期、并发、边界条件、外部输入）
   把相关代码读透，直到你能完整讲出这条路径。
4. **验证假设**：能用工具验证的，一定要验证。不要只凭阅读推断。
5. **记录发现**：每确认一个问题就立刻 record_finding，附上文件、行号与直接证据。
6. **收尾**：确认覆盖完范围后调用 finish。

# 判定标准

<severity>
- blocker：会导致数据错误、资源泄漏、服务不可用或安全问题，必须修。
- warn：在特定条件下会出错，或有明确缺陷但影响可控，应该修。
- note：可读性、健壮性上的改进，可以修。
</severity>

<evidence_rules>
- 只有能给出「文件 + 行号 + 直接证据（代码原文或工具输出）」时才记录发现。
- 不允许把「可能存在风险」「建议关注」这类主观推测登记为发现。
- 不允许编造行号。引用前先确认你确实读到过那一行。
- 如果一个疑点查证后不成立，就放弃它，不要为了凑数而记录。
- 缺陷的真实影响面要如实说明：只在特定条件下触发就写清楚条件，不要夸大。
</evidence_rules>

<out_of_scope>
以下内容默认不算发现，除非它直接导致缺陷：
- 命名、缩进、注释风格
- 个人偏好类的重构建议
- 第三方库自身的已知问题
</out_of_scope>

# 工具使用规范

<tool_rules>
- 引用代码位置一律用 `file:line` 格式。
- 一次可以并行发起多个独立调用（例如同时读三个文件），能省一轮往返。
- 工具报错时先读错误信息里的 `[建议]`，再决定是改参数重试还是换工具；不要在同一个
  参数上反复重试同一条失败调用。
- 上下文里没有的工具，用 discover_tools 检索后再调用；不要把「工具不存在」当作任务失败。
- 在需要判断「这类问题该按什么标准定级」时，用 read_skill 读审计方法论。
</tool_rules>

# 输出要求

- 面向读者的最终结论用中文，表述具体、可执行。
- 不要复述你已经通过工具展示过的代码，除非它构成关键证据。
- 不确定的地方要显式说明不确定性，不要用肯定的语气掩盖没验证的结论。
- 如果发现问题数为零，必须在 finish 的 summary 里说明你检查了哪些范围、为什么足以判定没有缺陷。
"""


# =============================================================================
# 上下文构造
# =============================================================================


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


def render_status(state: RunState, *, note: str = "") -> str:
    """把运行状态渲染成状态栏文本。

    书中的两条维护铁律：状态栏由**代码**维护，不让模型自己统计；
    并且它是对原始上下文的有损投影，只承载「代码算得准」的东西（计数、进度、时间）。
    """
    import datetime as _dt

    lines = [STATUS_OPEN, "Current State:"]

    done = sum(1 for todo in state.todos if todo.status == "completed")
    lines.append(f"- iteration: {state.iteration}/{state.max_iterations}")
    lines.append(f"- findings: {len(state.findings)} (todo_done={done}/{len(state.todos)})")

    counts = state.severity_counts()
    if state.findings:
        lines.append(
            f"- findings_by_severity: blocker={counts['blocker']} warn={counts['warn']} note={counts['note']}"
        )

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


def status_message(state: RunState, *, note: str = "") -> Message:
    """状态栏是一条 user 消息，固定放在轨迹最末尾。

    放末尾的原因：既不破坏前面的缓存前缀，又能获得最高的注意力权重。
    """
    return Message(role="user", content=render_status(state, note=note))


def goal_message(goal: str, context: str = "") -> Message:
    """首条 user 消息：审计目标。属于轨迹，不属于静态前缀。"""
    body = f"# 审计目标\n\n{goal.strip()}"
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


class ProtocolError(RuntimeError):
    """上下文违反了模型 API 的消息协议。属于 Harness 自身的编排错误，不该发生。"""


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


def findings_digest(findings: Sequence[Finding]) -> str:
    """给状态栏用的发现摘要（短，只列 id / 位置 / 标题）。"""
    if not findings:
        return "（暂无）"
    return "\n".join(
        f"- {item.id} [{item.severity}] {item.file}:{item.line} {item.title}" for item in findings[-12:]
    )


def todo_digest(todos: Sequence[Todo]) -> str:
    return "\n".join(todo.to_line() for todo in todos) or "（空）"
