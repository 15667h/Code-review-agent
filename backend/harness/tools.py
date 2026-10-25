"""工具接口层（ACI）。

设计依据（书中第四章）：
- 工具应对应 Agent 的**目标**，而不是底层 API 端点；粒度过细是反模式。
- 描述的核心是「什么时候用」，边界（做不到什么）比能力本身更重要 ——
  大多数工具选择错误的根因是模型不知道工具**不能**做什么。
- 参数用具体例子代替抽象规范；每个工具附 1~5 个真实调用示例。
- 长输出必须截断并落盘，且明确标示省略了多少、怎么读剩余部分。
- 执行类工具分层设防：输入校验 → 权限控制 → 沙箱。

工具用**英文名 + 英文描述**（对模型的工具选择更稳），
描述里同时写清楚何时不用，用来压低误触发。
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from .types import ToolCall, ToolResult

# =============================================================================
# 风险分级
# =============================================================================


class Risk(IntEnum):
    """执行层护栏的风险档位。高风险操作需额外审查或人工确认。"""

    LOW = 0  # 只读
    MEDIUM = 1  # 有副作用但可回滚（写文件、跑测试）
    HIGH = 2  # 不可逆或影响外部（推送、发送、部署）


# =============================================================================
# 输出截断
# =============================================================================

MAX_LINES = 200
MAX_CHARS = 10_000
HEAD_LINES = 50
TAIL_LINES = 50


def truncate_output(
    text: str,
    *,
    name: str = "output",
    save_dir: Path | None = None,
    workspace: Any = None,
    max_lines: int = MAX_LINES,
    max_chars: int = MAX_CHARS,
) -> tuple[str, bool, str | None]:
    """按书中的策略截断：保留头 50 行 + 尾 50 行，中间落盘。

    返回 ``(用于上下文的文本, 是否被截断, 落盘路径)``。

    ## 落盘位置必须在工作区内

    原实现落到系统临时目录，然后提示模型「用 read_file 读取该路径」——
    而 ``read_file`` 有工作区边界校验，**永远读不到那里**。
    实测后果：一次 PR 审查里 `git_diff` 输出被截断，模型按提示去读，
    拿到 ``PathEscape``，白丢一轮，而且错误信息里还泄露了绝对路径。

    所以调用方传 ``workspace`` 时，落盘到工作区内的 ``.margin-tmp/``；
    不传才退回临时目录（仅供离线脚本/测试使用）。
    """
    lines = text.splitlines()
    line_over = len(lines) > max_lines
    char_over = len(text) > max_chars
    if not (line_over or char_over):
        return text, False, None

    target_dir = save_dir or Path(tempfile.gettempdir()) / "margin-harness"
    # 传了 workspace 就落到它内部 —— 否则 read_file 读不回来（见函数注释）。
    if save_dir is None and workspace is not None:
        target_dir = Path(workspace.root) / ".margin-tmp"
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60] or "output"
    saved = target_dir / f"{safe_name}.txt"
    saved.write_text(text, encoding="utf-8")

    head = lines[:HEAD_LINES]
    tail = lines[-TAIL_LINES:]
    omitted = max(0, len(lines) - HEAD_LINES - TAIL_LINES)
    # 给模型看的路径：工作区内用相对路径（read_file 直接可用），
    # 否则给绝对路径（但那种情况下 read_file 读不回来，所以要标注）。
    if workspace is not None and save_dir is None:
        try:
            shown = saved.relative_to(Path(workspace.root)).as_posix()
            hint = f"[完整内容已保存至 {shown}，可用 read_file 读取]"
        except ValueError:
            hint = f"[完整内容已保存至 {saved}]"
    else:
        hint = f"[完整内容已保存至 {saved}]"
    marker = (
        f"\n... [省略 {omitted} 行 / 共 {len(lines)} 行，"
        f"{hint}] ...\n"
    )
    rendered = "\n".join(head) + marker + "\n" + "\n".join(tail)
    return rendered, True, str(saved)


# =============================================================================
# 轻量 JSON Schema 校验（不引入 jsonschema 依赖）
# =============================================================================

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """校验参数。**只报告问题，不做任何修正** —— 静默转换是书中明令禁止的反模式。"""
    errors: list[str] = []
    properties: dict[str, Any] = schema.get("properties") or {}

    for key in schema.get("required") or []:
        if key not in arguments:
            errors.append(f"缺少必填参数 `{key}`")

    for key, value in arguments.items():
        spec = properties.get(key)
        if spec is None:
            if schema.get("additionalProperties", False) is False:
                errors.append(f"不接受未知参数 `{key}`")
            continue

        expected = spec.get("type")
        if expected and not _matches_type(value, expected):
            detail = f"参数 `{key}` 应为 {expected}，实际是 {type(value).__name__}"
            if expected == "array":
                # 实测：模型最常把「只有一个条目的数组」写成裸字符串。错误信息必须
                # 直接给出目标形态，否则它下一轮还猜。拒绝但不静默转换，是纪律。
                detail += "；即使只有一项，也要写成 ['...'] 的形式，传字符串会被拒绝"
            errors.append(detail)
            continue

        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"参数 `{key}` 只能是 {spec['enum']} 之一，收到 {value!r}")

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                errors.append(f"参数 `{key}` 不得小于 {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                errors.append(f"参数 `{key}` 不得大于 {spec['maximum']}")

    return errors


def _matches_type(value: Any, expected: str) -> bool:
    types = _TYPE_MAP.get(expected)
    if types is None:
        return True
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, types)


# =============================================================================
# 工具定义
# =============================================================================

Handler = Callable[..., ToolResult]


@dataclass
class Tool:
    """一个可被模型调用的工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    risk: Risk = Risk.LOW
    # 一句话用途，用于工具目录与检索（渐进披露的第一层）
    summary: str = ""
    examples: list[dict[str, Any]] = field(default_factory=list)
    # 检索关键词：中英文都放，便于跨语言匹配
    keywords: tuple[str, ...] = ()

    def schema(self) -> dict[str, Any]:
        description = self.description.strip()
        if self.examples:
            rendered = json.dumps(self.examples, ensure_ascii=False, indent=2)
            description = f"{description}\n\nExamples:\n{rendered}"
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": self.parameters,
            },
        }

# =============================================================================
# 注册表
# =============================================================================


class ToolRegistry:
    """工具注册表。

    支持两级暴露（渐进披露）：
    1. ``core_schemas()`` 只给核心工具 + discover_tools 的 schema，进静态前缀；
    2. 其余工具的完整 schema 由模型主动检索后**追加到轨迹末尾**，静态前缀不受影响。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._core: set[str] = set()

    # ---- 注册 ----

    def register(self, tool: Tool, *, core: bool = False) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"工具 {tool.name} 已注册")
        self._tools[tool.name] = tool
        if core:
            self._core.add(tool.name)
        return tool

    def extend(self, tools: Iterable[Tool], *, core: bool = False) -> None:
        for tool in tools:
            self.register(tool, core=core)

    # ---- 查询 ----

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[name] for name in self.names()]

    def is_core(self, name: str) -> bool:
        return name in self._core

    def core_schemas(self) -> list[dict[str, Any]]:
        """静态前缀里的工具定义：只含核心工具 + 发现工具。**顺序固定**，不按热度排序。"""
        names = sorted(self._core)
        return [self._tools[name].schema() for name in names if name in self._tools]

    # ---- 检索（主动工具发现） ----

    def search(self, query: str, k: int = 5) -> list[Tool]:
        """BM25 检索。书中的实验：先检索再注入，比全量注入省 token 且选择更准。

        ``discover_tools`` 约定返回 3~5 个候选（书中实验 4-1 的取值）。
        """
        k = max(1, min(k, 5))
        if not query.strip():
            return self.all()[:k]

        query_terms = _tokenize(query)
        if not query_terms:
            return self.all()[:k]

        docs = {tool.name: _tokenize(_searchable(tool)) for tool in self.all()}
        avg_len = sum(len(d) for d in docs.values()) / max(1, len(docs))
        scored: list[tuple[float, Tool]] = []
        for tool in self.all():
            score = _bm25(query_terms, docs[tool.name], docs, avg_len)
            if score > 0:
                scored.append((score, tool))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [tool for _, tool in scored[:k]]

    # ---- 执行 ----

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult.failure(
                "UnknownTool",
                f"没有名为 `{call.name}` 的工具。",
                call.arguments,
                suggestion=f"可用工具：{', '.join(self.names())}。"
                "如果你在找某个能力，先用 discover_tools 检索。",
            )

        if "__unparsed__" in call.arguments:
            return ToolResult.failure(
                "MalformedArguments",
                "arguments 不是合法的 JSON 对象，无法解析。",
                {"raw": call.arguments["__unparsed__"][:200]},
                suggestion="请按 schema 重新给出参数，确保是合法 JSON。",
            )

        errors = validate_arguments(tool.parameters, call.arguments)
        if errors:
            return ToolResult.failure(
                "InvalidArguments",
                "参数校验未通过：" + "；".join(errors),
                call.arguments,
                suggestion=f"请对照 {tool.name} 的 schema 修正参数后重试。",
            )

        try:
            result = tool.handler(**call.arguments)
        except TypeError as exc:
            return ToolResult.failure(
                "BadSignature", f"调用签名不匹配：{exc}", call.arguments,
                suggestion="检查参数名是否与 schema 一致。",
            )
        except Exception as exc:  # noqa: BLE001 - 工具层兜底，避免单个工具打断整个循环
            import traceback

            return ToolResult.failure(
                type(exc).__name__,
                str(exc) or "工具执行时抛出异常。",
                call.arguments,
                traceback=traceback.format_exc(limit=6),
                suggestion="检查参数取值与目标路径是否存在。",
            )

        if not isinstance(result, ToolResult):
            return ToolResult.success(result)
        return result


# =============================================================================
# 检索辅助：BM25（中文按 2-gram，英文按词）
# =============================================================================

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    lowered = text.lower()
    terms = [match.group(0) for match in _TOKEN_RE.finditer(lowered)]
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", lowered)
    for run in cjk_runs:
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[i : i + 2] for i in range(len(run) - 1))
    return terms


def _searchable(tool: Tool) -> str:
    parts = [tool.name, tool.summary, tool.description, " ".join(tool.keywords)]
    return " ".join(parts)


def _bm25(
    query_terms: list[str],
    doc: list[str],
    all_docs: dict[str, list[str]],
    avg_len: float,
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    if not doc:
        return 0.0
    length_norm = 1 - b + b * (len(doc) / avg_len if avg_len else 1)

    from collections import Counter

    counts = Counter(doc)
    total_docs = len(all_docs)
    score = 0.0
    for term in query_terms:
        freq = counts.get(term, 0)
        if not freq:
            continue
        doc_freq = sum(1 for d in all_docs.values() if term in d)
        idf = math.log(1 + (total_docs - doc_freq + 0.5) / (doc_freq + 0.5))
        score += idf * (freq * (k1 + 1)) / (freq + k1 * length_norm)
    return score


# =============================================================================
# 发现工具（元工具）
# =============================================================================


DISCOVER_TOOL = Tool(
    name="discover_tools",
    summary="按需求检索可用工具，返回 3-5 个候选的完整 schema",
    description="""Load tool definitions on demand instead of keeping every schema in context.

Use this when:
- You need a capability that is not in your current tool list.
- You are unsure which tool fits a task, or want to compare a few candidates.

Do NOT use this when:
- The tool you need is already in your list (just call it).
- You only need to read/search files; those tools are always available.

The returned schemas are appended to the conversation. Once loaded, call the tool
directly in later turns; do not call discover_tools again for the same capability.

Returns: a list of 3-5 candidate tools, each with name, purpose and full parameter schema.""",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language description of the capability you need, "
                "e.g. 'run the project test suite' or '分析函数复杂度'.",
            },
            "limit": {
                "type": "integer",
                "description": "How many candidates to return, 1-5. Default 4.",
                "minimum": 1,
                "maximum": 5,
            },
        },
        "required": ["query"],
    },
    handler=lambda **_: ToolResult.failure("NotBound", "discover_tools 需在注册后由 registry 绑定。"),
    risk=Risk.LOW,
    keywords=("discover", "find", "tool", "capability", "检索", "工具", "发现", "能力"),
)


_RISK_LABEL = {Risk.LOW: "只读", Risk.MEDIUM: "写/执行", Risk.HIGH: "高风险"}


def tool_report(registry: ToolRegistry) -> str:
    """人类可读的工具清单，用于 CLI 展示。

    核心/按需的判定来自注册表本身，而不是某个 agent 写死的名单 ——
    否则给架构工具箱打标签时会全都标成「按需」。
    """
    lines = []
    for tool in registry.all():
        tag = _RISK_LABEL.get(tool.risk, "未知")
        core = "核心" if registry.is_core(tool.name) else "按需"
        lines.append(f"  {tool.name:<22} [{tag:<6}] [{core}]  {tool.summary}")
    return "\n".join(lines)


def bind_discovery(registry: ToolRegistry) -> Tool:
    """把 discover_tools 绑到具体注册表上。"""

    def handler(query: str, limit: int = 4) -> ToolResult:
        matches = registry.search(query, k=limit)
        if not matches:
            return ToolResult.failure(
                "NoToolFound",
                f"没有匹配 `{query}` 的工具。",
                {"query": query},
                suggestion="换一种说法描述需求，或直接用现有工具手工实现；"
                "也可以先用 run_command 处理。",
            )
        payload = [
            {"name": tool.name, "purpose": tool.summary or tool.description.splitlines()[0],
             "schema": tool.schema()["function"]}
            for tool in matches
        ]
        return ToolResult.success(
            "已加载以下工具，后续轮次可直接调用：\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )

    tool = Tool(
        name=DISCOVER_TOOL.name,
        description=DISCOVER_TOOL.description,
        parameters=DISCOVER_TOOL.parameters,
        handler=handler,
        risk=Risk.LOW,
        summary=DISCOVER_TOOL.summary,
        keywords=DISCOVER_TOOL.keywords,
    )
    return tool
