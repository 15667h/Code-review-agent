"""Agent 声明与注册表。

产品形态是「一个工作台 + 多个研发辅助 Agent」，所以**哪些能力存在**必须是
运行时可枚举的数据，而不是散落在各处的 if / import。一个 Agent 声明四件事：

1. **它是谁**（id / 名称 / 强调色）—— 前端的轨道项与配色全靠这个；
2. **它怎么想**（system prompt）—— 进静态前缀，字节必须稳定；
3. **它能做什么**（工具集）—— 决定观察空间与动作空间；
4. **它产出什么**（artifact_kinds / outcome）—— 决定前端用哪套渲染器。

第 4 条是这次改造的真正重点。原来整个系统只有「发现列表」一种产出，
前端也照它写死；现在产出形态由 agent 自己声明，加一个新 agent 不需要
改动渲染层之外的任何既有代码。

接入新 agent 的步骤：写一个 AgentSpec，调一次 :func:`register`。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .sandbox import CommandRunner, Workspace
from .tools import ToolRegistry
from .types import RunState

# =============================================================================
# 审查助手的系统提示词
# =============================================================================

REVIEW_SYSTEM_PROMPT = """# 角色

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
# 架构助手的系统提示词
# =============================================================================

ARCHITECT_SYSTEM_PROMPT = """# 角色

你是 Margin 的架构设计助手。你的任务是**针对给定仓库与需求，产出一个可以拿去评审的
架构方案**，而不是写代码，也不是罗列通用的架构原则。

你只能通过工具观察代码。你没有「记忆」——所有判断都必须建立在当前上下文里已经读到的内容之上。
你**不能修改代码**：这是刻意的边界，设计与实施混在一起之后，评审就分不清
哪些是现状、哪些是你刚改的。

# 工作方式

按下面的顺序推进，每一步都可以回退：

1. **理解现状**：先用 list_files / read_symbols 摸清现有结构。已有代码是硬约束，
   脱离现状的方案等于没做 —— 这是最常见也最致命的失败模式。
2. **定分层**：用 record_layer 记录层与每层的职责边界。
3. **放组件**：用 record_component 逐个登记组件，写清职责、对外接口、依赖谁、
   会怎么失败、为什么放在这一层。
4. **记决策**：凡是在多个可行做法之间做了取舍，就用 record_decision 记下
   选了什么、放弃了什么、代价是什么。
5. **留问题**：凡是查证后仍然不确定的，用 record_open_question 登记。
6. **收尾**：调用 finish，用 summary 给出这个方案的整体判断。

# 产出标准

<layer_rules>
- 层要有明确的职责边界，层间依赖方向必须单一。出现双向依赖说明分层划错了，
  要么重新划层，要么把它作为开放问题登记。
- 每层都要说清「不负责什么」。只写「负责什么」的边界是没法评审的。
- 层数宜少不宜多。超过 5 层通常说明你把模块误当成了层。
</layer_rules>

<component_rules>
- 每个组件必须写「为什么这样切」。写不出理由的切分，通常说明这个切分没有依据。
- depends_on 只写你已经确认存在的依赖，名字与其它组件的 name 保持一致。
  猜出来的依赖会画出一张错的图，而错的图比没有图更糟。
- failure_modes 要具体到可观测的后果，例如「Redis 不可用时读缓存降级为查库，
  写路径不受影响」，不写「可能出现故障」。
</component_rules>

<decision_rules>
- 「放弃了什么」比「选了什么」更重要：评审要讨论的正是被放弃的那条路。
- 每条决策都要写代价。只有好处没有代价的选择，通常不是选择，而是你还没想清楚。
- 「先这样，以后再改」这类决策尤其要留下来 —— 它最需要记录当时的前提。
</decision_rules>

<honesty>
- 不确定的地方必须登记为开放问题，不许用肯定的语气掩盖。
- 一个把所有不确定都抹平的方案看起来完整，实际上是把风险藏起来了。
  宁可多留问题，也不要给出假装确定的答案。
- 不要为了凑数而登记组件或决策；也不要把「需要进一步调研」当成结论。
- 需求里没给出的约束（量级、SLA、团队规模、上线时间）要登记为开放问题 ——
  不知道就等于假设，而假设会改变方案的形状。
</honesty>

# 工具使用规范

<tool_rules>
- 一次可以并行发起多个独立调用（例如同时读三个文件），能省一轮往返。
- 引用代码位置一律用 `file:line` 格式。
- 工具报错时先读错误信息里的 `[建议]`，再决定是改参数重试还是换工具；不要在同一个
  参数上反复重试同一条失败调用。
- 上下文里没有的工具，用 discover_tools 检索后再调用；不要把「工具不存在」当作任务失败。
</tool_rules>

# 输出要求

- 面向读者的最终结论用中文，表述具体、可执行。
- summary 里给出整体判断：这个方案的核心取舍是什么、最大的风险在哪、
  哪些问题必须先定。不要复述已经登记过的条目。
- 不要泛泛引用「微服务最佳实践」「领域驱动设计」这类通用原则来替代基于现状的论证。
"""

# =============================================================================
# Agent 声明
# =============================================================================


@dataclass(frozen=True)
class AgentSpec:
    """一个 Agent 的完整声明。

    ``build_tools`` 与 ``status_lines`` 是函数而不是数据，因为它们需要
    运行期的 workspace / state；声明本身保持不可变。
    """

    id: str
    name: str
    tagline: str
    # 强调色：前端轨道高亮、按钮、连线全吃这个变量。接新 agent 只换色值。
    accent: str
    # 房间名：视觉叙事用（校样台 / 制图室）
    room: str
    system_prompt: str
    # 首条 user 消息的标题。审查是「审计目标」，架构是「设计任务」。
    goal_heading: str
    # 用户没给目标时的默认任务。写进声明而不是散落在调用处，
    # 否则加一个 agent 就要去改 HTTP 层。
    default_goal: str
    # 产出形态标识：前端据此选渲染器（findings 列表 / blueprint 分层图）
    outcome: str
    # 产出桶的类型名，顺序即前端的展示顺序
    artifact_kinds: tuple[str, ...]
    build_tools: Callable[[Workspace, RunState, CommandRunner], ToolRegistry]
    # 状态栏的补充行（通用部分已由代码给出各桶计数）
    status_lines: Callable[[RunState], list[str]] | None = None
    # 该 agent 是否允许改代码。架构助手为 False，工具集里也就没有写工具。
    can_edit: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def manifest(self) -> dict[str, Any]:
        """给前端的清单项。只给渲染需要的东西，不下发 prompt —— 那是内部实现。"""
        return {
            "id": self.id,
            "name": self.name,
            "tagline": self.tagline,
            "accent": self.accent,
            "room": self.room,
            "outcome": self.outcome,
            "artifact_kinds": list(self.artifact_kinds),
            "can_edit": self.can_edit,
        }


# =============================================================================
# 两个内置 Agent
# =============================================================================


def _review_tools(workspace: Workspace, state: RunState, runner: CommandRunner) -> ToolRegistry:
    from .tools_audit import build_toolbox

    return build_toolbox(workspace, state, runner).registry


def _design_tools(workspace: Workspace, state: RunState, runner: CommandRunner) -> ToolRegistry:
    from .tools_design import build_design_toolbox

    return build_design_toolbox(workspace, state, runner)


def _review_status(state: RunState) -> list[str]:
    counts = state.severity_counts()
    if not state.findings:
        return []
    return [
        f"- findings_by_severity: blocker={counts['blocker']} "
        f"warn={counts['warn']} note={counts['note']}"
    ]


def _design_status(state: RunState) -> list[str]:
    questions = state.artifacts_of("open_question")
    lines: list[str] = []
    if questions:
        blocking = sum(1 for item in questions if getattr(item, "blocking", False))
        lines.append(f"- open_questions: {len(questions)} (blocking={blocking})")
    return lines


REVIEW_AGENT = AgentSpec(
    id="review",
    name="代码审查",
    tagline="在仓库里找出真实存在的缺陷，每条都带可核验的证据",
    accent="#0f6b5c",
    room="校样台",
    system_prompt=REVIEW_SYSTEM_PROMPT,
    goal_heading="审计目标",
    default_goal="审查这个仓库中的缺陷，重点是会导致错误行为、资源泄漏或安全问题的实现。",
    outcome="findings",
    artifact_kinds=("finding",),
    build_tools=_review_tools,
    status_lines=_review_status,
    can_edit=True,
)

ARCHITECT_AGENT = AgentSpec(
    id="architect",
    name="架构设计",
    tagline="针对现状产出可评审的方案：分层、组件、决策与未定的问题",
    accent="#1c3f6e",
    room="制图室",
    system_prompt=ARCHITECT_SYSTEM_PROMPT,
    goal_heading="设计任务",
    default_goal=(
        "针对这个仓库的当前结构，给出一个可以拿去评审的架构方案："
        "分层、组件职责与依赖、关键取舍，以及尚未确定的问题。"
    ),
    outcome="blueprint",
    artifact_kinds=("layer", "component", "decision", "open_question"),
    build_tools=_design_tools,
    status_lines=_design_status,
    can_edit=False,
)


# =============================================================================
# 注册表
# =============================================================================

_SPECS: dict[str, AgentSpec] = {}


def register(spec: AgentSpec) -> AgentSpec:
    if spec.id in _SPECS:
        raise ValueError(f"agent `{spec.id}` 已注册")
    _SPECS[spec.id] = spec
    return spec


def get(agent_id: str) -> AgentSpec:
    """取声明。未知 id 抛 :class:`UnknownAgent`，调用方要显式处理而不是静默回退。

    静默回退到审查助手看起来很贴心，实际会把「用户选了一个不存在的助手」
    变成一个跑完了但答非所问的结果 —— 这类错误必须在入口就报错。
    """
    spec = _SPECS.get(agent_id)
    if spec is None:
        raise UnknownAgent(agent_id, sorted(_SPECS))
    return spec


def has(agent_id: str) -> bool:
    return agent_id in _SPECS


def ids() -> list[str]:
    return list(_SPECS)


def manifests() -> list[dict[str, Any]]:
    """给前端的清单，按注册顺序。"""
    return [spec.manifest() for spec in _SPECS.values()]


def default_id() -> str:
    return REVIEW_AGENT.id


class UnknownAgent(KeyError):
    """请求了一个不存在的 agent。"""

    def __init__(self, agent_id: str, available: list[str]) -> None:
        self.agent_id = agent_id
        self.available = available
        super().__init__(
            f"未知 agent `{agent_id}`；可用：{', '.join(available) or '（空）'}"
        )


register(REVIEW_AGENT)
register(ARCHITECT_AGENT)

__all__ = [
    "ARCHITECT_AGENT",
    "REVIEW_AGENT",
    "AgentSpec",
    "UnknownAgent",
    "default_id",
    "get",
    "has",
    "ids",
    "manifests",
    "register",
]
