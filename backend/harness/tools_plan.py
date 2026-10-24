"""策划助手工具集。

本文件只放**计划特有**的工具（record_plan_step / record_plan_risk / check_plan_gaps）。
公共的读写检索在 ``tools_common``，代码图查询在 ``tools_graph``。

## 这个助手的产出为什么是第四种

前三个 agent 都在回答「现在怎样」：

- review：哪里**已经**坏了
- architect：结构**应该**怎么切
- tester：哪些行为**还没**被锁住

planner 回答的是「**要改成什么样、按什么顺序落地**」—— 唯一面向**未来**的产出。

## 两条硬约束，都来自「计划最常失败的方式」

**1. 每一步必须有可验收的完成条件（``done_when``）。**

计划的头号失败模式不是「步骤错了」，而是「步骤不可判定」：
「重构支付模块」「优化错误处理」—— 这类步骤写完之后，没人能说清做完没有。
所以 ``done_when`` 必填，并且要拒绝那些**只有动词没有判据**的写法。

**2. 每一步必须指到具体文件/符号（``touches``）。**

指不到位置的步骤，通常说明作者还没想清楚要改什么。
这是从 tester 那边学到的：「写不出 file:line 就是还没读懂」。
"""

from __future__ import annotations

import re

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
from .types import RunState, ToolResult


# =============================================================================
# 校验：done_when 必须是「可判定的」
# =============================================================================

#: 只有动作、没有判据的典型写法。这些词出现且**没有任何可观测名词**时视为空话。
_VAGUE_VERBS = (
    "优化", "重构", "改进", "提升", "梳理", "完善", "整理", "调整一下",
    "去掉", "删除", "修改", "处理", "搞定", "弄好",
)

#: 可判定的信号：出现这些说明作者给了可核对的判据。
#:
#: 刻意放宽 —— 宁可放过一条写得一般的，也不要把合法的判据写法拦掉。
#: 误杀（把合法写法判成空话）的代价比漏放高：模型会反复改措辞去绕校验，白烧轮次。
#:
#: 实测补过两类：
#: - **标识符**（``order.total`` / ``refund`` / ``_require_ops``）：出现具体标识符
#:   本身就是可核对的 —— 「不再读取 order.total」是可以去代码里查证的。
#: - **命令**（``ruff check`` / ``pytest`` / ``mypy``）：给了验证命令就是给了判据。
_CONCRETE_SIGNALS = re.compile(
    r"(\d+"                                        # 任何数字
    r"|测试|通过|失败|返回|抛出|抛错|报错|异常|等于|不等|大于|小于|包含|不含|不再|不会|不应"
    r"|全绿|green|passed|failed|==|!=|>=|<=|exit|输出|日志|字段|参数|接口|响应|状态码"
    r"|行\s*\d+|file:line|\.py|\.ts|\.go|\.js"
    # 标识符：xx.yy 或 下划线命名（order.total / _require_ops / get_user）
    r"|[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*"
    r"|\b[a-z]+_[a-z_]+\b"
    # 命令：pytest / ruff / mypy / pyright / npm / go test / git
    r"|pytest|ruff|mypy|pyright|eslint|npm|node|go\s+test|git\s)"
)


def _looks_unfalsifiable(text: str) -> bool:
    """判断一个「完成条件」是不是空话（无法判定真假）。

    判据：短、且只有动作词、且**没有任何可观测信号**。

    为什么不能只查关键词：合法写法里也会有「修改」两个字
    （「修改 refund 的金额来源为 paid，并让 6 条测试通过」）。
    所以必须同时满足「没有任何可观测信号」才判为空话 ——
    宁可放过，不可误杀（误杀会让模型反复改措辞，浪费轮次）。
    """
    t = (text or "").strip()
    # 先查信号再看长度。顺序反了会误杀短但合法的写法：
    # 「6 条测试全绿」只有 7 个字符，却是一个完全可判定的完成条件。
    # 上一版把长度判断放在前面，导致这个最常见的合法写法被拒 ——
    # 模型收到「无法判定真假」的错误提示，会去改措辞而不是改内容。
    if _CONCRETE_SIGNALS.search(t):
        return False
    if len(t) < 4:
        return True
    # 没有任何可观测信号，且以动作词开头或包含动作词 → 空话
    return any(v in t for v in _VAGUE_VERBS)


# =============================================================================
# 工具：登记一步
# =============================================================================


def _tool_record_plan_step(state: RunState) -> Tool:
    def handler(
        order: int,
        title: str,
        done_when: str,
        why: str = "",
        touches: list[str] | None = None,
        verify: str = "",
        may_invalidate: str = "",
        evidence: str = "",
    ) -> ToolResult:
        from .types import PlanStep

        # ---- 校验 ①：完成条件必须可判定 ----
        if not (done_when or "").strip():
            return ToolResult.failure(
                "MissingDoneWhen",
                "缺少 done_when：这一步怎样才算做完？",
                {"title": title},
                suggestion="写成可以判真假的条件，例如「6 条 service 测试全绿」"
                           "或「refund 不再读取 order.total」。写不出就说明这一步还没想清楚。",
            )
        if _looks_unfalsifiable(done_when):
            return ToolResult.failure(
                "UnfalsifiableDoneWhen",
                f"done_when 无法判定真假：「{done_when}」。"
                "它没有给出任何可观测的信号（数字、测试结果、返回值、报错、字段名等）。",
                {"done_when": done_when},
                suggestion="补一个可核对的判据。例如把「优化错误处理」改成"
                           "「所有外部调用都包在 try/except 里，且新增 3 条异常路径测试通过」。",
            )

        # ---- 校验 ②：必须指到具体位置 ----
        places = [str(x).strip() for x in (touches or []) if str(x).strip()]
        if not places:
            return ToolResult.failure(
                "MissingTouches",
                "缺少 touches：这一步要改哪些文件/符号？",
                {"title": title},
                suggestion="至少给一个具体路径或符号名（例如 `app/service.py` 或 `refund`）。"
                           "指不到位置的步骤，通常说明还没想清楚要改什么。",
            )
        # 形如 `app/` 这种整目录也算「没指到」——它等于「改整个模块」
        if all(p.endswith("/") for p in places):
            return ToolResult.failure(
                "VagueTouches",
                f"touches 只给了目录（{places}），没指到文件或符号。",
                {"touches": places},
                suggestion="细化到文件或符号，例如 `app/service.py` 或 `OrderService.refund`。",
            )

        # ---- 校验 ③：顺序必须连续 ----
        existing = state.artifacts_of("plan_step")
        orders = sorted(int(getattr(x, "order", 0)) for x in existing)
        if existing and order not in orders:
            expected = (max(orders) + 1) if order > max(orders) else order
            if order != expected:
                return ToolResult.failure(
                    "OrderGap",
                    f"步骤序号不连续：现有 {orders}，你给的 order={order}。",
                    {"order": order, "existing": orders},
                    suggestion=f"按依赖顺序逐步登记，下一步应该是 order={max(orders) + 1}。"
                               "序号断裂会让读者误以为漏了步骤。",
                )

        step = PlanStep(
            order=order,
            title=title,
            why=why,
            touches=places,
            done_when=done_when.strip(),
            verify=verify,
            may_invalidate=may_invalidate,
            evidence=evidence,
        )
        state.add_artifact("plan_step", step)
        total = len(state.artifacts_of("plan_step"))
        without_verify = sum(
            1 for x in state.artifacts_of("plan_step") if not (getattr(x, "verify", "") or "").strip()
        )
        summary = f"当前共 {total} 步"
        if without_verify:
            summary += f"（其中 {without_verify} 步没给验证手段）"
        return ToolResult.success(f"已登记第 {order} 步：{title}\n{summary}")

    return Tool(
        name="record_plan_step",
        summary="登记计划的一步（order/title/done_when/touches 必填）",
        description="""登记实施计划里的一步。

何时用：
- 你已经读过相关代码，能说清「这一步改哪里、改成什么样、怎样算做完」。

何时不用：
- 只是有个大概想法、还没读代码 → 先读，再回来登记。
- 想报告代码缺陷 → 那是代码审查的活儿。
- 想描述目标架构 → 那是架构设计的活儿（本工具是「怎么落地」，不是「应该长什么样」）。

边界：
- **`done_when` 必须可判定**：要给出可观测的判据（测试结果、返回值、报错、字段名、数字）。
  只有动作词没有判据的写法会被拒（例如「优化错误处理」「重构支付模块」）。
- **`touches` 必须指到文件或符号**，只给目录（`app/`）会被拒。
- **`order` 必须连续**：按依赖顺序从 1 开始，不能跳号。
- 同一 order + title 不会重复登记。

Returns: 记录结果与当前累计步数。""",
        parameters={
            "type": "object",
            "properties": {
                "order": {
                    "type": "integer",
                    "description": "第几步，从 1 开始、按依赖顺序连续递增",
                    "minimum": 1,
                },
                "title": {
                    "type": "string",
                    "description": "这一步做什么，动词开头，一句话。例如「把 refund 的金额来源改为 paid」",
                },
                "done_when": {
                    "type": "string",
                    "description": "**必填**：怎样算做完。必须是可判真假的条件，"
                                   "例如「6 条 service 测试全绿」「refund 不再读取 order.total」",
                },
                "touches": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "**必填**：涉及的文件或符号，数组，每项一个，"
                                   "例如 [\"app/service.py\", \"OrderService.refund\"]",
                },
                "why": {"type": "string", "description": "为什么需要这一步（不做会怎样）"},
                "verify": {
                    "type": "string",
                    "description": "怎么验证（跑什么命令 / 看什么现象），例如「python -m pytest tests/test_service.py」",
                },
                "may_invalidate": {
                    "type": "string",
                    "description": "这一步可能推翻前面哪些步骤的假设；写不出说明步骤间没有真实依赖",
                },
                "evidence": {
                    "type": "string",
                    "description": "读了哪处代码得出「要改这里」，格式 `file:line`",
                },
            },
            "required": ["order", "title", "done_when", "touches"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[
            {
                "order": 1,
                "title": "把 refund 的可退金额来源从 order.total 改为 order.paid",
                "done_when": "refund(paid=0, total=100) 抛错或返回 0；现有 6 条 service 测试全绿",
                "touches": ["app/service.py", "OrderService.refund"],
                "why": "当前按应付计算，未付款订单也能全额退款",
                "verify": "python -m pytest tests/test_service.py -q",
                "evidence": "app/service.py:181",
            }
        ],
        keywords=("plan", "step", "todo", "计划", "步骤", "落地", "实施"),
    )


# =============================================================================
# 工具：登记风险/前提
# =============================================================================


def _tool_record_plan_risk(state: RunState) -> Tool:
    def handler(assumption: str, if_wrong: str, blocking: bool = False) -> ToolResult:
        from .types import OpenQuestion

        if not (assumption or "").strip():
            return ToolResult.failure("EmptyAssumption", "前提不能为空。", {},
                                      suggestion="写出你这条计划依赖的假设，"
                                                 "例如「调用方只有 tests 里的 3 处」。")
        if not (if_wrong or "").strip():
            return ToolResult.failure(
                "MissingIfWrong",
                "缺少 if_wrong：这个前提不成立时会发生什么？",
                {"assumption": assumption},
                suggestion="说明后果与应对，例如「还有 2 处生产调用点，需要一并改」"
                           "或「整个计划顺序要重排」。",
            )

        # 复用 OpenQuestion 的数据形状：都是「我不确定的、需要人判的」东西。
        # 但用独立的产出桶（plan_risk），因为语义不同：这是**假设**，不是**问题**。
        item = OpenQuestion(
            question=assumption.strip(),
            why_it_matters=if_wrong.strip(),
            blocking=bool(blocking),
        )
        state.add_artifact("plan_risk", item)
        total = len(state.artifacts_of("plan_risk"))
        blocking_count = sum(
            1 for x in state.artifacts_of("plan_risk") if getattr(x, "blocking", False)
        )
        return ToolResult.success(
            f"已记录前提：{assumption}\n当前共 {total} 条（其中 {blocking_count} 条会阻塞计划）"
        )

    return Tool(
        name="record_plan_risk",
        summary="登记计划依赖的前提，以及它不成立时的后果",
        description="""记录这条计划依赖的一个前提假设，以及假设不成立时的后果。

何时用：
- 你的计划建立在某个**你没能完全确认**的假设上（调用方只有这些、数据量不会很大、某接口没被别处用到）。
- 某个不确定项一旦不成立，整个计划的顺序要重排。

何时不用：
- 已经用工具确认过的事实 → 那是证据，不是前提。
- 想登记「实现步骤」→ 用 record_plan_step。
- 想问一个开放问题但没有具体假设 → 也写这里，但要把假设写具体。

边界：
- `if_wrong` 必填：只说「有这个风险」没有信息量，要说清「不成立会怎样、要怎么调整」。
- `blocking=true` 表示「这个前提没定下来，计划就不能开始」。
- 与架构助手的开放问题区别：**这是假设**（我当前按它做计划），
  架构那边是**问题**（我还没想好怎么切）。

Returns: 记录结果与当前前提总数。""",
        parameters={
            "type": "object",
            "properties": {
                "assumption": {"type": "string", "description": "你依赖的前提（一句话）"},
                "if_wrong": {
                    "type": "string",
                    "description": "**必填**：前提不成立时的后果与应对",
                },
                "blocking": {
                    "type": "boolean",
                    "description": "这个前提没确认就不该开始实施，默认 false",
                },
            },
            "required": ["assumption", "if_wrong"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{
            "assumption": "refund 只被 OrderService 内部调用，外部没有直接调用点",
            "if_wrong": "若有外部调用点，需要先加兼容层或同步改，否则改了签名会静默坏掉",
            "blocking": True,
        }],
        keywords=("risk", "assumption", "premise", "风险", "前提", "假设", "依赖"),
    )


# =============================================================================
# 工具：计划完整性自检
# =============================================================================


def _tool_check_plan_gaps(state: RunState) -> Tool:
    """机械自检：当前计划有哪些明显缺口。

    和 tester 的 ``check_coverage`` 是同一个思路 —— **确定性判定不交给模型**。
    模型写完计划容易漏掉「有步骤但没验证手段」「步骤指向的文件不存在」这类结构问题，
    而这个工具能把它们直接列出来。
    """

    def handler() -> ToolResult:
        steps = sorted(state.artifacts_of("plan_step"), key=lambda x: getattr(x, "order", 0))
        if not steps:
            return ToolResult.success("还没有登记任何步骤。先用 record_plan_step 登记第一步。")

        problems: list[str] = []

        # ① 序号连续性
        orders = [getattr(s, "order", 0) for s in steps]
        expected = list(range(1, len(orders) + 1))
        if orders != expected:
            problems.append(f"步骤序号不连续：{orders}（应为 {expected}）")

        # ② 没有验证手段的步骤
        no_verify = [s for s in steps if not (getattr(s, "verify", "") or "").strip()]
        if no_verify:
            names = "、".join(f"第 {getattr(s, 'order', '?')} 步" for s in no_verify)
            problems.append(f"{len(no_verify)} 步没给 verify（怎么确认做完了）：{names}")

        # ③ 没有 why 的步骤（为什么需要这一步）
        no_why = [s for s in steps if not (getattr(s, "why", "") or "").strip()]
        if no_why:
            names = "、".join(f"第 {getattr(s, 'order', '?')} 步" for s in no_why)
            problems.append(f"{len(no_why)} 步没写 why（不做会怎样）：{names}")

        # ④ 没有证据的步骤
        no_ev = [s for s in steps if not (getattr(s, "evidence", "") or "").strip()]
        if no_ev:
            names = "、".join(f"第 {getattr(s, 'order', '?')} 步" for s in no_ev)
            problems.append(f"{len(no_ev)} 步没给 evidence（读了哪处代码得出要改这里）：{names}")

        # ⑤ 依赖风险有没有登记
        blocking = sum(1 for r in state.artifacts_of("plan_risk") if getattr(r, "blocking", False))
        if not state.artifacts_of("plan_risk"):
            problems.append("没有登记任何前提/风险 —— 一个「零假设」的计划通常说明你还没真正评估过不确定性")

        lines = [f"当前计划共 {len(steps)} 步。"]
        if not problems:
            lines.append("结构自检没发现缺口（这**不代表**计划是对的，只代表格式齐全）。")
        else:
            lines.append(f"发现 {len(problems)} 处缺口：")
            for i, p in enumerate(problems, 1):
                lines.append(f"  {i}. {p}")
        if blocking:
            lines.append(f"\n注意：有 {blocking} 条阻塞性前提还没确认 —— 计划落地前必须先定。")
        return ToolResult.success("\n".join(lines))

    return Tool(
        name="check_plan_gaps",
        summary="机械自检当前计划的缺口（缺验证、缺理由、序号断裂等）",
        description="""检查已登记的计划有哪些结构性缺口。

何时用：
- 收尾之前自检一遍，看有没有漏掉「验证手段」「为什么需要这一步」。
- 登记了若干步之后，想确认序号和依赖顺序没乱。

何时不用：
- 想判断计划**对不对**（这工具只看格式，不看内容正确性）。
- 还没登记任何步骤。

边界：
- **只做结构检查**：序号连续、字段齐全。它**不判断**步骤是否合理、
  优先级是否对、有没有漏掉关键步骤。
- 自检通过 ≠ 计划可行。不要把这个结论当成质量保证。

Returns: 步数、缺口清单、阻塞性前提数量。""",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        risk=Risk.LOW,
        examples=[{}],
        keywords=("check", "gap", "self check", "检查", "缺口", "自检", "完整性"),
    )


# =============================================================================
# 组装
# =============================================================================

CORE_TOOLS: tuple[str, ...] = (
    "list_files",
    "read_file",
    "grep",
    "git_diff",
    "read_symbols",
    "record_plan_step",
    "update_todo",
    "finish",
    "discover_tools",
)


def build_plan_toolbox(workspace: Workspace, state: RunState,
                       runner: CommandRunner) -> ToolRegistry:
    """构建策划助手工具箱。

    与其它三个的差别：
    - **没有 record_finding / record_test_case**（产出是 plan_step + plan_risk）
    - **没有 apply_patch**：计划是「建议怎么改」，不是「已经改好了」
    - 多了 check_plan_gaps（机械自检）
    - 代码图查询全给（``impact`` 是它最常用的：估改动波及范围）
    """
    from .tools import bind_discovery
    from .tools_graph import build_graph_toolbox

    registry = ToolRegistry()

    registry.register(bind_discovery(registry), core=True)
    registry.register(_tool_list_files(workspace), core=True)
    registry.register(_tool_read_file(workspace), core=True)
    registry.register(_tool_grep(workspace), core=True)
    registry.register(_tool_git_diff(workspace, runner), core=True)
    registry.register(_tool_read_symbols(workspace), core=True)
    registry.register(_tool_record_plan_step(state), core=True)
    registry.register(_tool_update_todo(state), core=True)
    registry.register(_tool_finish(state, outcome="roadmap"), core=True)

    # 按需加载
    registry.register(_tool_record_plan_risk(state))
    registry.register(_tool_check_plan_gaps(state))
    registry.register(_tool_read_skill(workspace))

    # 代码图：只读查询（策划不该建索引，复用审查助手建好的那份）
    for tool in build_graph_toolbox(workspace, readonly=True).all():
        if registry.get(tool.name) is None:
            registry.register(tool)

    return registry


__all__ = ["build_plan_toolbox", "CORE_TOOLS"]
