"""架构设计工具集。

与审查工具集的**边界差异是刻意的**：架构助手只做只读观察 + 记录，
不给 apply_patch / run_tests / lint_file —— 它产出设计，不修改代码。
一个能顺手改代码的设计助手会让「设计」和「实施」的边界消失，
评审时也就分不清哪些是现状、哪些是你刚改的。

产出工具（record_*）对应原型里的四块内容：
分层图 / 组件清单 / 决策记录 / 开放问题。
依赖（连线）不单独记录：它写在组件的 ``depends_on`` 里，
由前端据此推导 —— 图和清单若分开录入，就必然出现对不上的一天。
"""

from __future__ import annotations

from .sandbox import CommandRunner, Workspace
from .tools import Risk, Tool, ToolRegistry
from .tools_common import (
    _tool_finish,
    _tool_git_diff,
    _tool_grep,
    _tool_list_files,
    _tool_read_file,
    _tool_read_skill,
    _tool_read_symbols,
    _tool_update_todo,
)
from .types import Component, DecisionRecord, Layer, OpenQuestion, RunState, ToolResult

# =============================================================================
# 公共产出工具的返回文案
# =============================================================================

def _tally(state: RunState, kind: str) -> int:
    return len(state.artifacts_of(kind))

def _accepted(
    kind: str, label: str, state: RunState, *, unit: str = "条", note: str = ""
) -> ToolResult:
    """登记成功后的统一回复：确认收到 + 当前累积量。

    给累积量而不是只说「已记录」，是为了让模型能从状态里看出进度，
    不必靠自己数调用了几次（状态栏由代码维护，这条原则在这里同样适用）。
    """
    tail = f"\n{note}" if note else ""
    return ToolResult.success(
        f"已记录{label}。当前共 {_tally(state, kind)} {unit}{label}。{tail}".rstrip()
    )

def _duplicate(
    kind: str, label: str, state: RunState, key: str, *, unit: str = "条"
) -> ToolResult:
    """同名条目被去重：如实告知，不要假装记下了。"""
    return ToolResult.success(
        f"{label}「{key}」已存在（同名视为同一{unit}，未重复登记）。"
        f"当前共 {_tally(state, kind)} {unit}{label}。\n"
        "如果你是想修正它的内容，请用不同的名字重新登记，并在 finish 的 summary 里说明。",
    )

# =============================================================================
# 分层
# =============================================================================

def _tool_record_layer(state: RunState) -> Tool:
    def handler(
        name: str,
        purpose: str = "",
        hint: str = "",
        order: int = 0,
        evidence: list[str] | None = None,
    ) -> ToolResult:
        layer = Layer(
            name=name, purpose=purpose, hint=hint, order=order,
            evidence=list(evidence or []),
        )
        if not state.add_artifact("layer", layer):
            return _duplicate("layer", "分层", state, name)
        return _accepted("layer", "分层", state)

    return Tool(
        name="record_layer",
        summary="登记一个架构分层及其职责边界",
        description="""Record one architectural layer: its name, what it owns, and what it must not do.

何时用：
- 你已经看清系统的职责边界，准备把它划成若干层时。
- 层的划分本身就是方案的关键结论 —— 划层等于在说「依赖只能朝这个方向走」。

何时不用：
- 只是想描述某个模块的内部结构：那是一个组件，用 record_component。
- 还没读完代码就先划层：分层是从现状里读出来的，不是从教科书里抄来的。

边界：
- 层数宜少不宜多。超过 5 层通常说明你把「模块」也误当成了「层」。
- purpose 写「负责什么」，hint 写「不负责什么 / 现在的取舍」。边界靠后者才说得清。
- order 给一个递增整数表示自上而下的顺序；同 order 的层按登记顺序排。
- 同名分层只保留一条。

Returns: 确认文本，含当前已登记的分层数。""",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "层名，例如 '接入层' / 'Application'"},
                "purpose": {"type": "string", "description": "这一层负责什么，一句话"},
                "hint": {"type": "string", "description": "这一层不负责什么，或当前的取舍与约束"},
                "order": {"type": "integer", "description": "自上而下的层序，小的在上。默认 0", "minimum": 0, "maximum": 20},
                "evidence": {
                    "type": "array",
                    "description": (
                        "数组，每项是一个完整的 `相对路径:行号`，证明这一层确实存在。"
                        "例如 [\"src/pluggy/_manager.py:19\", \"src/pluggy/_hooks.py:1\"]。"
                        "必须写全路径（不要只写文件名）—— 只有文件名时无法核验。"
                    ),
                    "items": {"type": "string"},
                },
            },
            "required": ["name"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[
            {"name": "接入层", "purpose": "协议转换与鉴权，不含业务逻辑", "hint": "先单体内的路由，不急着上网关", "order": 0},
        ],
        keywords=("layer", "tier", "architecture", "分层", "架构", "层次"),
    )

# =============================================================================
# 组件
# =============================================================================

def _norm(name: str) -> str:
    """组件名归一：与前端 blueprint.js 的 norm 同规则（trim + 小写）。

    两端必须同规则 —— 后端按归一匹配来「纠正」名字，前端按归一来「找到」节点，
    两套规则必然对不上。
    """
    return str(name or "").strip().lower()

def _tool_record_component(state: RunState) -> Tool:
    def handler(
        name: str,
        layer: str = "",
        role: str = "",
        responsibility: str = "",
        interfaces: list[str] | None = None,
        depends_on: list[str] | None = None,
        failure_modes: list[str] | None = None,
        rationale: str = "",
        evidence: list[str] | None = None,
    ) -> ToolResult:
        # 依赖名对齐：实测模型会写 'Session' / 'session' 两种形式，前端按归一找节点，
        # 名字漂移的边就静默消失了。这里在后端一次性对齐到已登记组件的规范名。
        existing = state.artifacts_of("component")
        canonical = {_norm(c.name): c.name for c in existing}
        canonical[_norm(name)] = name  # 自己刚登记的也算已知

        self_deps: list[str] = []
        unknown: list[str] = []
        aligned: list[str] = []
        for dep in depends_on or []:
            key = _norm(dep)
            if key == _norm(name):
                self_deps.append(dep)  # 自己依赖自己没有信息量，还会画出一条怪边
                continue
            if key in canonical:
                aligned.append(canonical[key])
            else:
                aligned.append(dep)  # 允许后登记的依赖，但要在回复里点名
                unknown.append(dep)

        component = Component(
            name=name,
            layer=layer,
            role=role,
            responsibility=responsibility,
            interfaces=list(interfaces or []),
            depends_on=aligned,
            failure_modes=list(failure_modes or []),
            rationale=rationale,
            evidence=list(evidence or []),
        )
        if not state.add_artifact("component", component):
            return _duplicate("component", "组件", state, name, unit="个")

        # 依赖指向未登记的组件是允许的（模型可能后登记它），但要提醒一句 ——
        # 前端画连线时找不到目标就会少一条边，这种「图缺了一块」很难事后发现。
        notes = []
        if self_deps:
            notes.append(f"已剔除自依赖 {', '.join(self_deps)}：组件不依赖自己。")
        if unknown:
            notes.append(
                f"注意：依赖 {', '.join(unknown)} 尚未登记为组件，请用**完全一致**的名字补上。"
            )
        return _accepted("component", "组件", state, unit="个", note=" ".join(notes))

    return Tool(
        name="record_component",
        summary="登记一个组件：职责、接口、依赖、失败模式与切分理由",
        description="""Record a component (service / module / store) with what it does, what it exposes,
what it depends on, how it fails, and why it is cut this way.

何时用：
- 每确定一个组件就登记一条，不要攒到最后一起写。
- 组件是评审的主要对象，它的字段就是评审要问的问题。

何时不用：
- 只是描述一层的整体职责：用 record_layer。
- 还没确认依赖是否真实存在：先去读代码确认。猜出来的依赖会画出一张错的图。

边界：
- depends_on 只写**你已经确认存在**的依赖。名字按「忽略大小写与首尾空格」自动对齐到
  已登记组件的规范名（写 'session' 也能连到 'Session'）；对不上就会少一条边，
  工具会在回复里点名那些尚未登记的名字。自己依赖自己会被直接剔除。
- failure_modes 要具体：写「Redis 不可用时读缓存降级为查库，写路径不受影响」，
  不写「可能出现故障」。
- rationale 是必填的实质内容。「为什么这样切」写不出来，通常说明这个切分没有依据。
- 同名组件只保留一条。

Returns: 确认文本，含当前组件数与未登记依赖的提醒。""",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "组件名，例如 'order-service' / '订单服务'"},
                "layer": {"type": "string", "description": "所属层名，需与 record_layer 的 name 一致"},
                "role": {"type": "string", "description": "一句话角色，例如 '持久化' / '防腐层'"},
                "responsibility": {"type": "string", "description": "职责说明：它做什么"},
                "interfaces": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "数组，每项一个字符串。例如 ['POST /orders', 'GET /orders/{id}']；只有一项也要写成数组 ['...']",
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "数组，每项一个组件的 name，例如 ['mysql', 'redis']；没有依赖就给空数组 []",
                },
                "failure_modes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "数组，每项写一种失效后果，要具体到可观测。例如 ['Redis 不可用时读缓存降级为查库，写路径不受影响']",
                },
                "rationale": {"type": "string", "description": "为什么这样切：依据是什么、替代切法为什么不选"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "数组，每项一个完整的 `相对路径:行号`，证明这个组件确实存在。例如 ['src/pluggy/_manager.py:19']。必须写全路径（不要只写文件名），否则事后无法核验",
                },
            },
            "required": ["name"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[
            {
                "name": "inventory",
                "layer": "领域层",
                "role": "库存扣减",
                "responsibility": "维护 SKU 可用库存，提供扣减与回滚",
                "interfaces": ["POST /stock:deduct", "POST /stock:release"],
                "depends_on": ["mysql", "redis"],
                "failure_modes": ["Redis 不可用时热点 SKU 扣减退化为串行，吞吐下降但不超卖"],
                "rationale": "扣减是强一致操作，与订单的编排逻辑分离后才能独立扩容",
            }
        ],
        keywords=("component", "service", "module", "组件", "服务", "模块", "依赖"),
    )

# =============================================================================
# 决策记录
# =============================================================================

def _tool_record_decision(state: RunState) -> Tool:
    def handler(
        title: str,
        decision: str = "",
        rationale: str = "",
        alternatives: list[str] | None = None,
        consequences: str = "",
        evidence: list[str] | None = None,
    ) -> ToolResult:
        record = DecisionRecord(
            title=title,
            decision=decision,
            rationale=rationale,
            alternatives=list(alternatives or []),
            consequences=consequences,
            evidence=list(evidence or []),
        )
        if not state.add_artifact("decision", record):
            return _duplicate("decision", "决策", state, title)
        return _accepted("decision", "决策", state)

    return Tool(
        name="record_decision",
        summary="登记一条设计决策：选了什么、放弃了什么、代价是什么",
        description="""Record a design decision, including the option you rejected and what it costs.

何时用：
- 凡是在多个可行做法之间做了取舍，都要记一条。取舍才是需要评审的东西。
- 尤其是「先这样，以后再改」这类决策 —— 它最需要留下当时的理由。

何时不用：
- 只是描述现状（例如「当前用的是 MySQL」）：那是组件信息，不是决策。
- 没有任何备选的方案：那不是决策，是唯一解，写进 rationale 即可。

边界：
- alternatives 写**被放弃**的做法，最好带上放弃的原因。
  评审要讨论的正是这条路，只写「选了什么」等于把讨论的前提藏起来。
- consequences 必须写代价。只有好处没有代价的选择，通常说明你还没想清楚。
- 同名决策只保留一条。

Returns: 确认文本，含当前决策数。""",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "决策标题，例如 '订单与库存之间用事务消息'"},
                "decision": {"type": "string", "description": "最终选择：具体做法是什么"},
                "rationale": {"type": "string", "description": "为什么选它：依据、前提、触发条件"},
                "alternatives": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "数组，每项一个被放弃的备选方案（建议带上放弃原因）。"
                    "只有一项也必须写成数组 ['...']，传字符串会被拒绝。没有备选就给空数组 []",
                },
                "consequences": {"type": "string", "description": "这个选择的代价与后续影响"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "数组，每项一个完整的 `相对路径:行号`，证明这条决策有代码依据。必须写全路径（不要只写文件名），否则事后无法核验",
                },
            },
            "required": ["title"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[
            {
                "title": "订单与库存之间用事务消息而非分布式事务",
                "decision": "订单落库与发消息在同一本地事务内，库存侧保证幂等消费",
                "rationale": "当前量级下 2PC 的协调成本高于重试成本",
                "alternatives": ["Seata AT 模式：引入额外协调组件，运维复杂度高"],
                "consequences": "库存侧必须实现幂等，且短暂不一致需要监控能观测到",
            }
        ],
        keywords=("decision", "adr", "tradeoff", "决策", "取舍", "权衡", "方案"),
    )

# =============================================================================
# 开放问题
# =============================================================================

def _tool_record_open_question(state: RunState) -> Tool:
    def handler(
        question: str,
        why_it_matters: str = "",
        blocking: bool = False,
        evidence: list[str] | None = None,
    ) -> ToolResult:
        item = OpenQuestion(
            question=question,
            why_it_matters=why_it_matters,
            blocking=blocking,
            evidence=list(evidence or []),
        )
        if not state.add_artifact("open_question", item):
            return _duplicate("open_question", "开放问题", state, question)
        return _accepted("open_question", "开放问题", state)

    return Tool(
        name="record_open_question",
        summary="登记一个尚未确定的问题及其影响",
        description="""Record a question the design has NOT settled, and why it matters.

何时用：
- 查证后仍然不确定的地方，一律登记。这是这个助手最不该省略的一步。
- 输入端没给出的约束（量级、SLA、团队规模、上线时间）也要登记 ——
  它们会改变方案的形状，不知道就等于假设。

何时不用：
- 已经查证并确定的事情：写进组件的 rationale 或决策的 rationale。
- 纯粹的实现细节（变量名、目录结构）：不值得占用评审的注意力。

边界：
- blocking=True 表示「不定这个就没法定方案」；评审应当先解决这类问题。
- why_it_matters 要写清楚它会改变什么结论，而不是重复一遍问题。
- 一个把所有不确定都抹平的方案看起来完整，实际上是把风险藏起来了。
  宁可多留问题，也不要给出假装确定的答案。
- 同名问题只保留一条。

Returns: 确认文本，含当前开放问题数。""",
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "尚未确定的问题"},
                "why_it_matters": {"type": "string", "description": "它会影响哪个结论、改变什么取舍"},
                "blocking": {"type": "boolean", "description": "是否在方案落地前必须先定，默认 false"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "数组，每项一个完整的 `相对路径:行号`，指向让你产生这个疑问的代码位置（读到什么、没找到什么）。必须写全路径",
                },
            },
            "required": ["question"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[
            {
                "question": "预期峰值 QPS 是多少？",
                "why_it_matters": "决定订单写入是否需要分库，进而决定库存能否共库",
                "blocking": True,
            }
        ],
        keywords=("question", "unknown", "risk", "问题", "待定", "不确定", "风险"),
    )

# =============================================================================
# 组装
# =============================================================================

CORE_TOOLS = (
    "list_files",
    "read_file",
    "grep",
    "read_symbols",
    "record_layer",
    "record_component",
    "record_decision",
    "record_open_question",
    "update_todo",
    "finish",
    "discover_tools",
)

def build_design_toolbox(
    workspace: Workspace, state: RunState, runner: CommandRunner
) -> ToolRegistry:
    """构建架构设计工具箱。

    核心工具进静态前缀（含四个产出工具 —— 它们必须随叫随到，
    否则模型会为了省事把结论攒在最后一次性输出，中间过程就丢了）。
    """
    from .tools import bind_discovery

    registry = ToolRegistry()

    registry.register(bind_discovery(registry), core=True)
    # sketch 进核心：它是「先看全局再判断」的入口，如果藏进 discover_tools，
    # 模型会照旧逐个 read_file，那这条改进就等于没做。
    registry.register(_tool_sketch(workspace), core=True)
    registry.register(_tool_list_files(workspace), core=True)
    registry.register(_tool_read_file(workspace), core=True)
    registry.register(_tool_grep(workspace), core=True)
    # read_symbols 对架构是核心能力：看符号表比读全文更快建立结构认知
    registry.register(_tool_read_symbols(workspace), core=True)
    registry.register(_tool_record_layer(state), core=True)
    registry.register(_tool_record_component(state), core=True)
    registry.register(_tool_record_decision(state), core=True)
    registry.register(_tool_record_open_question(state), core=True)
    registry.register(_tool_update_todo(state), core=True)
    # 收尾统计按产出形态给（架构报各类型条数，不报「0 条发现」）
    registry.register(_tool_finish(state, outcome="blueprint"), core=True)

    registry.register(_tool_git_diff(workspace, runner))
    # 刻意不注册 run_command：架构助手声明 can_edit=False（只读设计）。
    # run_command 是 HIGH 风险（白名单命令可代写文件、git checkout、跑脚本），
    # 用户打开高风险开关后执行层并不拦它 —— 边界要靠「不给工具」保证，
    # 而不是靠「给了但希望不被用」。要看变更用 git_diff，要看结构用
    # list_files / read_symbols / grep。run_agent 里的 _enforce_read_only
    # 会把这条声明变成硬检查：哪天有人又注册进来，运行直接失败。
    registry.register(_tool_read_skill(workspace))

    return registry

__all__ = ["CORE_TOOLS", "build_design_toolbox"]

# =============================================================================
# 架构速写（两阶段里的第一阶段：先拿结构，再判断）
# =============================================================================

def _tool_sketch(workspace: Workspace) -> Tool:
    def handler(path: str = ".") -> ToolResult:
        from .sketch import render, sketch_repo

        try:
            base = workspace.resolve(path)
        except PathEscape as exc:
            return ToolResult.failure("PathEscape", str(exc), {"path": path})
        if not base.is_dir():
            return ToolResult.failure(
                "NotADirectory",
                f"不是一个目录：{path}",
                {"path": path},
                suggestion="用 list_files('.') 先看目录结构。",
            )

        sketches = sketch_repo(base)
        if not sketches:
            return ToolResult.success(
                f"在 {path} 下没有找到可解析的 Python 文件。\n"
                "如果这个仓库主要是其它语言，请用 list_files + read_file 逐个看。"
            )
        return ToolResult.success(render(sketches))

    return Tool(
        name="sketch",
        summary="一次扫描出仓库的结构清单：每个文件的行数、类、函数、内部依赖",
        description="""Sketch the repository structure in ONE call: for every Python file, its line count,
classes, functions and internal dependencies.

何时用：
- **开工第一步就该用它**。先拿到全局结构，再决定读哪几个文件 ——
  逐个 read_file 摸结构很慢，而且很容易在读完之前就用完轮次预算。
- 准备分层或划组件之前，用它确认「仓库里到底有哪些模块」。

何时不用：
- 需要看具体实现细节：速写只有符号名，没有函数体，那要用 read_file。
- 仓库不是 Python：这个工具只解析 Python 的 ast。

边界：
- 只做结构提取，不做架构判断 —— 它不会告诉你该怎么分层。
- 默认最多列 120 个文件，超出会明说还剩多少。
- 语法错误的文件会被跳过（那不是架构问题）。

Returns: 紧凑的结构清单文本。""",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要扫描的目录（相对工作区），默认 '.' 即整个仓库",
                }
            },
            "required": [],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{"path": "."}, {"path": "src"}],
        keywords=("sketch", "structure", "overview", "outline", "速写", "结构", "概览", "目录"),
    )
