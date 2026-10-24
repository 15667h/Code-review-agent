"""代码图工具集：把 ``codegraph`` 的三个查询暴露给 Agent。

## 为什么单独一个文件

公共的读写检索工具在 ``tools_common``，两个 agent 共用；这里是**跨 agent 共享的结构查询**，
未来 tester / guide / triage 都会用到，所以独立成一份，谁需要就 ``build_graph_toolbox``。

## 渐进披露

这四个工具**不进静态前缀**（``core=False``）。理由：它们依赖索引、且只在特定问题上才需要，
塞进前缀会稀释模型对核心工具（read_file / grep）的注意力，还破坏缓存前缀的字节稳定性。
模型通过 ``discover_tools`` 检索到它们的 schema 后再用。

## 一条重要纪律：拿不到索引时不假装

索引可能还没建（新仓库、或建图失败）。这时工具**明确返回「索引不存在，请先建图」**，
而不是返回空列表 —— 空列表会被模型理解成「没有任何调用方」，那是**错误的事实**，
会导致它得出「这个函数没人用，可以随便改」的结论。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .codegraph import GRAPH_DIR, CodeGraph
from .sandbox import Workspace
from .tools import Risk, Tool, ToolRegistry
from .types import ToolResult


# =============================================================================
# 索引定位
# =============================================================================


def graph_path(workspace: Workspace, runs_dir: Path | None = None) -> Path:
    """索引文件位置：``<runs_dir>/codegraph/<仓库路径hash>.db``。

    放在归档目录而不是工作区，原因见 ``codegraph.GRAPH_DIR`` 的注释：
    放工作区里既污染审查范围，又会让只读 agent 拿到中风险工具。
    按仓库路径 hash 分文件名，这样同一个服务审多个仓库时互不覆盖。
    """
    base = Path(runs_dir) if runs_dir else Path(".runs")
    digest = hashlib.sha256(str(workspace.root.resolve()).encode("utf-8")).hexdigest()[:12]
    return base / GRAPH_DIR / f"{digest}.db"


def open_graph(workspace: Workspace) -> tuple[CodeGraph | None, str]:
    """打开索引。返回 ``(图, 错误说明)``，二者必有其一为空。"""
    path = graph_path(workspace)
    if not path.is_file():
        return None, (
            "这个仓库还没有建代码图。先调用 build_graph（会扫描全部 Python 文件并建索引，通常 1~3 秒），"
            "再用 who_calls / impact / tests_for 查询。"
        )
    graph = CodeGraph(path)
    from .codegraph import SCHEMA_VERSION
    if graph.schema_version() != SCHEMA_VERSION:
        graph.close()
        return None, (
            f"索引版本过期（现有 {graph.schema_version()}，需要 {SCHEMA_VERSION}）。"
            "重新调用 build_graph 重建即可。"
        )
    return graph, ""


def _not_found_message(symbol: str, candidates: list[str], tool: str) -> str:
    """符号查不到时的返回文本。

    必须给出候选而不是只说「没找到」：实测里模型用工具名 ``apply_patch`` 去查，
    实际函数叫 ``_tool_apply_patch`` —— 只说没找到，模型就会放弃这条工具路径，
    而它本来是对的。给候选等于把它引到正确的名字上。
    """
    lines = [f"索引里没有名为 `{symbol}` 的符号。"]
    if candidates:
        lines.append("最接近的几个符号：" + "、".join(f"`{c}`" for c in candidates))
        lines.append("如果其中一个就是你要找的，用它重新调用本工具。")
    else:
        lines.append(
            "没有任何名字包含这个串的符号。可能原因：① 名字拼错；"
            "② 它是动态生成的（getattr / 注册表）；③ 它在非 Python 文件里。"
        )
        if tool == "tests_for":
            lines.append("想确认是否真的没有测试，可以用 grep 直接搜符号名。")
    return "\n".join(lines)


# =============================================================================
# 工具
# =============================================================================


def _tool_build_graph(workspace: Workspace) -> Tool:
    def handler(force: bool = False) -> ToolResult:
        path = graph_path(workspace)
        if path.is_file() and not force:
            graph = CodeGraph(path)
            version = graph.schema_version()
            graph.close()
            from .codegraph import SCHEMA_VERSION
            if version == SCHEMA_VERSION:
                return ToolResult.success(
                    f"代码图已存在（{path.relative_to(workspace.root).as_posix()}），无需重建。"
                    "直接调用 who_calls / impact / tests_for 查询；"
                    "仓库改动较大时可传 force=true 重建。"
                )

        graph = CodeGraph(path)
        try:
            stats = graph.build(workspace.root)
        finally:
            graph.close()

        body = [f"已建图：{stats.describe()}，耗时 {stats.elapsed:.2f}s。"]
        # 语言清点：让模型立刻知道「这个仓库我能做到什么程度」
        if stats.languages:
            body.append("")
            body.append(stats.language_summary())
        if stats.errors:
            body.append("以下文件没能解析（不参与索引）：")
            for err in stats.errors[:8]:
                body.append(f"  - {err}")
            if len(stats.errors) > 8:
                body.append(f"  …… 另有 {len(stats.errors) - 8} 个")
        body.append("")
        body.append(
            "注意：这是**静态**结构图。Python 的动态调用（getattr / 注册表 / 装饰器包装）"
            "无法从语法读到，不会出现在结果里；推断出来的边会标注置信度。"
            "**非 Python 语言不进符号图** —— 对它们只能做规则扫描，"
            "who_calls / impact / tests_for 返回空不代表没有调用方。"
        )
        return ToolResult.success("\n".join(body))

    return Tool(
        name="build_graph",
        summary="为当前仓库建立代码结构索引（符号 + 调用/导入/测试边）",
        description="""扫描仓库里的 Python 文件，建立「谁定义了什么、谁调用了谁」的结构索引。

何时用：
- 需要跨文件追踪调用关系、但仅靠 grep 找不到（比如改了函数签名想知道会坏哪）。
- 第一次在这个仓库上用 who_calls / impact / tests_for 之前（它们需要索引）。

何时不用：
- 只是读一个已知路径的文件 → 用 read_file。
- 想按文本内容找东西 → 用 grep，它更快且不需要索引。
- 仓库不是 Python 项目 → 本索引只支持 Python，调用会返回「没有可索引的文件」。

边界：
- **只支持 Python**（.py）。其他语言的文件不参与索引。
- 已存在且版本一致时直接跳过；force=true 才重建。
- 建图耗时与文件数成正比（本项目 114 个文件约 1.2 秒）。

Returns: 扫描文件数、符号数、边数（其中多少是直接读到的、多少是推断的）、耗时与失败文件清单。""",
        parameters={
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "已存在索引时是否强制重建，默认 false（跳过）",
                },
            },
        },
        handler=handler,
        # 它会写索引文件（虽然写在归档目录、不在被审工作区）。
        # 标 MEDIUM 而不是 LOW 是准确的：一个「只读」agent 不该拥有建索引的能力，
        # 因为建索引会消耗磁盘与时间，且结果会改变后续查询的答案。
        risk=Risk.MEDIUM,
        examples=[{}, {"force": True}],
        keywords=("graph", "index", "build", "代码图", "索引", "建图", "结构"),
    )


def _tool_who_calls(workspace: Workspace) -> Tool:
    def handler(symbol: str, limit: int = 30) -> ToolResult:
        graph, err = open_graph(workspace)
        if graph is None:
            return ToolResult.failure(
                "NoGraphIndex", err, {"symbol": symbol},
                suggestion="先调用 build_graph 建索引。",
            )
        try:
            rows = graph.who_calls(symbol, limit=limit)
            found = graph.find(symbol)
            # 候选名必须在连接关闭前取出来
            candidates = graph.suggest(symbol) if not found else []
        finally:
            graph.close()

        if not found:
            return ToolResult.success(_not_found_message(symbol, candidates, "who_calls"))
        if not rows:
            names = "、".join(f["qname"] for f in found[:5])
            return ToolResult.success(
                f"找到符号（{names}），但**没有任何调用方边**。\n"
                "这不等于「没人用」：也可能调用的地方是动态调用（getattr / 注册表 / 装饰器），"
                "静态解析读不到。确认无人使用前建议再用 grep 搜一次函数名。"
            )

        lines = [f"`{symbol}` 的调用方（共 {len(rows)} 条，按置信度排序）："]
        for r in rows[:limit]:
            mark = "直接读到" if r["origin"] == "EXTRACTED" else f"推断 conf={r['confidence']:.2f}"
            loc = f"{r['caller_file']}:{r['caller_line']}" if r["caller_file"] else "?"
            lines.append(f"  {r['caller']}  ({loc})  [{mark}]")
        if len(rows) >= limit:
            lines.append(f"[... 达到 {limit} 条上限，可调大 limit 或改用更具体的符号名]")
        return ToolResult.success("\n".join(lines))

    return Tool(
        name="who_calls",
        summary="查一个符号被谁调用（跨文件反向调用图）",
        description="""查询「谁调用了这个符号」。跨文件的反向调用图，grep 做不到的事。

何时用：
- 要改函数签名 / 返回值结构，想知道会坏哪些地方。
- 审查某个工具函数时，想确认它的调用方是否处理了异常或边界。

何时不用：
- 只想知道符号定义在哪 → 用 read_symbols 或 read_file。
- 想按名字找文本出现的位置（包括注释、字符串、动态调用）→ 用 grep。

边界：
- **只支持 Python**，且依赖 build_graph 建好的索引。
- 静态分析：**动态调用（getattr / 字典注册表 / 装饰器包装 / **kwargs 转发）读不到**。
  结果里没有某个调用方，不代表它不存在。
- 每条边标注来源：`直接读到` 是语法里明确写的；`推断 conf=0.xx` 是通过 import 表或同名匹配推出来的，可能猜错。
- 同名符号（三个类里都有 `run()`）请用限定名 `Class.run` 指定。

Returns: 每行 `调用方限定名 (文件:行) [来源]`。""",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "符号名。可用裸名 `read_file`，或用限定名 `Class.method` / `mod.func` 消歧",
                },
                "limit": {"type": "integer", "description": "最多返回多少条，默认 30", "minimum": 1, "maximum": 200},
            },
            "required": ["symbol"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{"symbol": "apply_patch"}, {"symbol": "Workspace.resolve", "limit": 10}],
        keywords=("who calls", "callers", "reverse", "调用方", "谁调用", "反向", "影响"),
    )


def _tool_impact(workspace: Workspace) -> Tool:
    def handler(symbol: str, depth: int = 2) -> ToolResult:
        graph, err = open_graph(workspace)
        if graph is None:
            return ToolResult.failure("NoGraphIndex", err, {"symbol": symbol},
                                      suggestion="先调用 build_graph 建索引。")
        try:
            rings = graph.impact(symbol, depth=depth)
            found = graph.find(symbol)
            candidates = graph.suggest(symbol) if not found else []
        finally:
            graph.close()

        if not found:
            return ToolResult.success(_not_found_message(symbol, candidates, "impact"))
        if not rings:
            return ToolResult.success(
                f"`{symbol}` 没有影响到其他符号（没有调用方边）。\n"
                "注意动态调用读不到，改动前建议 grep 确认。"
            )

        lines = [f"改动 `{symbol}` 的影响范围（按距离分组）："]
        for label, items in rings.items():
            lines.append(f"\n{label} —— {len(items)} 处")
            for it in items[:25]:
                loc = f"{it['file']}:{it['line']}" if it.get("file") else "?"
                lines.append(f"  {it['caller']}  ({loc})")
            if len(items) > 25:
                lines.append(f"  …… 另有 {len(items) - 25} 处")
        lines.append(
            "\n这是**静态**影响范围。动态调用与反射读不到，"
            "所以不能当作「改这些地方就安全」的保证，只是缩小人工排查的范围。"
        )
        return ToolResult.success("\n".join(lines))

    return Tool(
        name="impact",
        summary="查改动一个符号会波及哪些地方（按距离分三档）",
        description="""查询「如果改这个符号，哪些地方会受影响」，结果按距离分三档。

何时用：
- 评审一个改动时，要给出「波及范围」，而不是只说「这里有问题」。
- 判断一个重构是否安全：先看 d=1 有多少处。

何时不用：
- 只关心直接调用方 → 用 who_calls 更简洁。
- 想知道某个功能怎么实现的 → 用 grep 或 read_file。

边界：
- **只支持 Python**，依赖 build_graph 索引。
- 分档含义：`d=1` 直接调用方（改了签名**一定**坏）；`d=2` 间接依赖（**很可能**受影响）；
  `d=3` 传递影响（**需要跑测试**确认）。
- 静态分析：动态调用读不到。**这不是「改这些就安全」的保证**，只是把人工排查范围缩小。
- 三档的措辞是刻意的：d=1 说「一定」，d=2 说「很可能」，d=3 说「需要确认」——
  不要把推断当成事实。

Returns: 按 d=1 / d=2 / d=3 分组的受影响符号，每行 `限定名 (文件:行)`。""",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "符号名（裸名或限定名）"},
                "depth": {"type": "integer", "description": "向下追几层，默认 2；3 会更全但更慢", "minimum": 1, "maximum": 4},
            },
            "required": ["symbol"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{"symbol": "Workspace.resolve"}, {"symbol": "build_toolbox", "depth": 3}],
        keywords=("impact", "blast", "radius", "影响", "波及", "范围", "依赖"),
    )


def _tool_tests_for(workspace: Workspace) -> Tool:
    def handler(symbol: str, limit: int = 30) -> ToolResult:
        graph, err = open_graph(workspace)
        if graph is None:
            return ToolResult.failure("NoGraphIndex", err, {"symbol": symbol},
                                      suggestion="先调用 build_graph 建索引。")
        try:
            rows = graph.tests_for(symbol, limit=limit)
            found = graph.find(symbol)
            candidates = graph.suggest(symbol) if not found else []
        finally:
            graph.close()

        if not found:
            return ToolResult.success(_not_found_message(symbol, candidates, "tests_for"))
        if not rows:
            return ToolResult.success(
                f"索引里**没有找到覆盖 `{symbol}` 的测试**。\n"
                "两种可能：① 真的没有测试；② 测试通过动态方式调用（如 pytest 参数化、"
                "fixture 间接调用），静态读不到。\n"
                "建议用 grep 搜一下符号名，确认是否真的无覆盖。"
            )
        lines = [f"覆盖 `{symbol}` 的测试（共 {len(rows)} 处）："]
        for r in rows[:limit]:
            loc = f"{r['file']}:{r['line']}" if r.get("file") else "?"
            lines.append(f"  {r['test']}  ({loc})")
        lines.append(
            "\n这些是「调用了该符号的测试函数」，不是「一定断言了它的行为」。"
            "要确认覆盖质量，需要读测试内容。"
        )
        return ToolResult.success("\n".join(lines))

    return Tool(
        name="tests_for",
        summary="查一个符号被哪些测试覆盖（用于修复后验证）",
        description="""查询「哪些测试覆盖了这个符号」。改完代码后决定跑哪些测试时用。

何时用：
- 修好一个缺陷后，想知道该跑哪些测试来证明没坏别的。
- 评估一个模块的测试覆盖情况。

何时不用：
- 想跑测试本身 → 用 run_tests。
- 想知道测试文件里写了什么 → 用 read_file。

边界：
- **只支持 Python**，依赖 build_graph 索引。
- 返回的是「**调用了**该符号的测试函数」，不等于「**断言了**它的行为」。
  覆盖质量需要读测试内容才能判断 —— 不要把这个列表当成覆盖率的证明。
- 通过 pytest 参数化、fixture、conftest 间接调用的，静态可能读不到。

Returns: 每行 `测试函数限定名 (文件:行)`。""",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "符号名（裸名或限定名）"},
                "limit": {"type": "integer", "description": "最多返回多少条，默认 30", "minimum": 1, "maximum": 100},
            },
            "required": ["symbol"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{"symbol": "json_fetch"}, {"symbol": "Workspace.resolve"}],
        keywords=("test", "coverage", "tests for", "测试", "覆盖", "用例"),
    )


# =============================================================================
# 工具箱
# =============================================================================


def build_graph_toolbox(workspace: Workspace, *, readonly: bool = False) -> ToolRegistry:
    """代码图工具集。**不设核心工具**：全部按需检索后加载（渐进披露）。

    ``readonly=True`` 时不提供 ``build_graph``：只读 agent（架构助手）不该拥有
    写盘能力，哪怕写的是归档目录。它仍可查询**已存在**的索引 ——
    索引由审查助手或用户预先建好即可，两个 agent 共用同一份图。
    """
    registry = ToolRegistry()
    tools = [
        _tool_who_calls(workspace),
        _tool_impact(workspace),
        _tool_tests_for(workspace),
    ]
    if not readonly:
        tools.insert(0, _tool_build_graph(workspace))
    registry.extend(tools, core=False)
    return registry
