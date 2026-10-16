"""离线启发式模型。

这不是 mock。它**真的**调用同一套工具、真的读文件、真的产出发现，
只是「决策」来自确定性规则而不是 LLM。三个用途：

1. 在没有 API Key 的情况下验证 harness 全链路（上下文构造 / 工具执行 / 护栏 / 报告）；
2. 作为评估的对照组基线 —— 如果 LLM 审计连规则都跑不赢，说明 Harness 或提示词有问题；
3. 作为回归测试的执行体，让 loop 的改动可以被自动化测试覆盖。

它严格地只依据**上下文里已经出现的内容**做决策（解析 read_file 的返回），
不去直接读磁盘 —— 否则就破坏了「Agent 只能看到上下文里的东西」这一前提，
测出来的东西也就没有说服力了。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from .rules import RULES, scan_source
from .types import Decision, Message, ToolCall

_LINE_PREFIX = re.compile(r"^\s*\d+\s\|\s?(.*)$")
_FILE_ENTRY = re.compile(r"^(.+?)\s+\(\d+\s*B\)$")
# 只取「顶层包名」：它是把文件归到某个组件的依据
_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))")

AUDIT_AGENT = "review"
DESIGN_AGENT = "architect"


def _last_tool_output(messages: Sequence[Message]) -> str:
    for message in reversed(messages):
        if message.role == "tool":
            return message.content
    return ""


def _parse_file_list(text: str) -> list[str]:
    files: list[str] = []
    for line in text.splitlines():
        match = _FILE_ENTRY.match(line.strip())
        if match:
            files.append(match.group(1).strip())
    return files


def _strip_line_numbers(text: str) -> tuple[list[str], str]:
    """把 read_file 的带行号输出还原成源码行。

    返回 ``(源码行列表, 头部说明)``。头部说明用于判断是否被截断。
    """
    lines: list[str] = []
    header = ""
    for line in text.splitlines():
        match = _LINE_PREFIX.match(line)
        if match:
            lines.append(match.group(1))
        elif not lines and not header:
            header = line
    return lines, header


class HeuristicModel:
    """用规则驱动工具链的离线模型。

    按 agent 分派：审查走规则扫描，架构走结构盘点。
    两条路都**真的**调用同一套工具、真的读文件、真的产出，只是决策来自确定性规则。
    """

    name = "heuristic-offline"

    def __init__(self, *, max_files: int = 12, agent: str = AUDIT_AGENT) -> None:
        self.max_files = max_files
        self.agent = agent
        self.step = 0
        self.files: list[str] = []
        self._current_pair = 0
        self.reported: set[str] = set()
        # 架构模式的状态
        self.layers: list[tuple[str, str, str]] = []
        self.components: list[tuple[str, str]] = []
        self.deps: dict[str, set[str]] = {}
        self._file_count: dict[str, int] = {}
        self._read_index = 0
        self._awaiting_read = False
        self._components_recorded = False
        self._todos_closed = False

    # ------------------------------------------------------------------ #

    @property
    def usage_tokens(self) -> dict[str, int]:
        """离线模型不消耗真实 token；返回零值以便统计口径统一。"""
        return {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0}

    def complete(
        self,
        messages: Sequence[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> Decision:
        if self.agent == DESIGN_AGENT:
            return self._complete_design(messages)
        return self._complete_audit(messages)

    # ------------------------------------------------------------------ #

    def _complete_audit(self, messages: Sequence[Message]) -> Decision:
        self.step += 1
        last = _last_tool_output(messages)

        # 1) 先列计划
        if self.step == 1:
            return _call(
                ToolCall(
                    "update_todo",
                    {
                        "todos": [
                            {"text": "枚举待审源文件", "status": "in_progress"},
                            {"text": "逐文件读取并检查", "status": "pending"},
                            {"text": "记录发现并收尾", "status": "pending"},
                        ]
                    },
                ),
                reasoning="离线模式：先确定范围，再逐文件检查，最后记录发现。",
            )

        # 2) 枚举文件
        if self.step == 2:
            return _call(
                ToolCall("list_files", {"path": ".", "pattern": "*.py", "limit": 60}),
                reasoning="枚举 Python 源文件。",
            )

        # 3) 解析文件列表
        if self.step == 3:
            self.files = _parse_file_list(last)[: self.max_files]
            if not self.files:
                return _call(
                    ToolCall(
                        "finish",
                        {"summary": "仓库中没有找到 Python 源文件，本次审计未覆盖任何代码。"},
                    ),
                    reasoning="没有可审的目标。",
                )
            return _call(
                ToolCall(
                    "update_todo",
                    {
                        "todos": [
                            {"text": f"枚举待审源文件（{len(self.files)} 个）", "status": "completed"},
                            {"text": "逐文件读取并检查", "status": "in_progress"},
                            {"text": "记录发现并收尾", "status": "pending"},
                        ]
                    },
                ),
                reasoning=f"共 {len(self.files)} 个文件待查。",
            )

        # 4) 逐文件：读 → 检查
        index = self._current_pair
        if index >= len(self.files):
            return self._finish_decision()

        if self.step % 2 == 0:
            return _call(
                ToolCall("read_file", {"path": self.files[index], "offset": 1, "limit": 600}),
                reasoning=f"读取第 {index + 1} 个文件：{self.files[index]}",
            )

        # 检查刚读到的内容
        path = self.files[index]
        source_lines, header = _strip_line_numbers(last)
        self._current_pair += 1

        if not source_lines:
            return _call(
                ToolCall("update_todo", {"todos": _todos(self.files, index + 1, len(self.reported))}),
                reasoning=f"{path} 读取为空，跳过。",
            )

        source = "\n".join(source_lines)
        hits = scan_source(path, source)
        calls: list[ToolCall] = []
        for hit in hits:
            if hit.fingerprint in self.reported:
                continue
            self.reported.add(hit.fingerprint)
            calls.append(
                ToolCall(
                    "record_finding",
                    {
                        "severity": hit.rule.severity,
                        "file": hit.path,
                        "line": hit.line,
                        "title": f"{hit.rule.title}（{hit.rule.id}）",
                        "detail": hit.rule.detail,
                        "evidence": hit.evidence,
                        "suggestion": hit.rule.suggestion,
                    },
                )
            )

        if calls:
            return Decision(tool_calls=calls, content="", reasoning=f"{path} 命中 {len(calls)} 条规则。")

        truncated = "还有" in header and "未显示" in header
        note = "（该文件被分页，本次只检查了前 600 行）" if truncated else ""
        return _call(
            ToolCall(
                "update_todo",
                {"todos": _todos(self.files, index + 1, len(self.reported))},
            ),
            reasoning=f"{path} 未命中规则{note}。",
        )

    # ------------------------------------------------------------------ #

    def _finish_decision(self) -> Decision:
        return _call(
            ToolCall(
                "finish",
                {
                    "summary": (
                        f"离线规则模式检查了 {len(self.files)} 个 Python 文件，"
                        f"记录 {len(self.reported)} 条问题。"
                        "该结论来自确定性规则集，只覆盖规则能表达的模式"
                        "（密钥硬编码、SQL 拼接、异常吞掉、句柄未关闭、可变默认参数等），"
                        "不包含跨文件数据流与业务逻辑缺陷 —— 用真实模型跑时覆盖面会更广。"
                    )
                },
            ),
            reasoning="所有文件已检查完，收尾。",
        )

    # ================================================================== #
    # 架构模式：结构盘点
    # ================================================================== #

    def _complete_design(self, messages: Sequence[Message]) -> Decision:
        """架构模式只做**结构盘点**，不做设计判断。

        规则能可靠地告诉你「现在的模块边界在哪、谁 import 了谁」；
        它不能告诉你「这样切对不对」。所以这里如实登记前者，
        并把后者作为 blocking 的开放问题留出来 —— 而不是编一个看起来完整的方案。
        """
        self.step += 1
        last = _last_tool_output(messages)

        if self.step == 1:
            return _call(
                ToolCall("update_todo", {"todos": _design_todos("盘点现有结构", 0, 0)}),
                reasoning="离线模式：先盘点现有结构，再登记层与组件。",
            )

        if self.step == 2:
            return _call(
                ToolCall("list_files", {"path": ".", "pattern": "*.py", "limit": 120}),
                reasoning="枚举 Python 源文件。",
            )

        if self.step == 3:
            self.files = _parse_file_list(last)
            if not self.files:
                return _call(
                    ToolCall(
                        "finish",
                        {"summary": "仓库中没有找到 Python 源文件，无法盘点结构。"},
                    ),
                    reasoning="没有可盘点的目标。",
                )
            self._plan_structure()
            calls = [
                ToolCall(
                    "record_layer",
                    {"name": name, "purpose": purpose, "hint": hint, "order": index},
                )
                for index, (name, purpose, hint) in enumerate(self.layers)
            ]
            calls.append(
                ToolCall("update_todo", {"todos": _design_todos("读取模块依赖", len(self.layers), 0)})
            )
            return Decision(
                tool_calls=calls,
                content="",
                reasoning=f"按目录边界划出 {len(self.layers)} 层（由目录名推断）。",
            )

        targets = self._read_targets()

        # 逐个读文件：一次发读、下一次解析它返回的源码
        if self._read_index < len(targets):
            path = targets[self._read_index]
            if self._awaiting_read:
                self._awaiting_read = False
                self._collect_imports(path, last)
                self._read_index += 1
                return _call(
                    ToolCall(
                        "update_todo",
                        {"todos": _design_todos("读取模块依赖", len(self.layers), self._read_index)},
                    ),
                    reasoning=f"{path} 的 import 已收集。",
                )
            self._awaiting_read = True
            return _call(
                ToolCall("read_file", {"path": path, "offset": 1, "limit": 400}),
                reasoning=f"读取 {path}，收集它对其它模块的依赖。",
            )

        # 登记完组件后先把任务列表收干净，否则收尾核验会报「有未完成项却没说明」
        if self._components_recorded and not self._todos_closed:
            self._todos_closed = True
            return _call(
                ToolCall(
                    "update_todo",
                    {
                        "todos": [
                            {"text": f"盘点现有结构（{len(self.layers)} 层）", "status": "completed"},
                            {"text": f"读取模块依赖（{self._read_index}）", "status": "completed"},
                            {"text": f"登记组件与开放问题（{len(self.components)} 个）", "status": "completed"},
                        ]
                    },
                ),
                reasoning="全部登记完成，收尾。",
            )

        if not self._components_recorded:
            self._components_recorded = True
            calls = [
                ToolCall(
                    "record_component",
                    {
                        "name": name,
                        "layer": layer,
                        "role": "现有模块",
                        "responsibility": f"包含 {self._file_count.get(name, 0)} 个源文件",
                        "depends_on": sorted(self.deps.get(name, set())),
                        "failure_modes": [],
                        "rationale": "按现有目录边界登记（离线模式只盘点现状，不判断切分是否合理）",
                    },
                )
                for name, layer in self.components
            ]
            calls.append(
                ToolCall(
                    "record_open_question",
                    {
                        "question": "这些目录边界是否就是期望的服务边界？",
                        "why_it_matters": (
                            "离线规则模式只能按目录与 import 盘点现状，"
                            "无法判断哪些边界应当保留、哪些应当拆分或合并"
                        ),
                        "blocking": True,
                    },
                )
            )
            return Decision(tool_calls=calls, content="", reasoning="登记组件与开放问题。")

        return self._finish_design()

    def _plan_structure(self) -> None:
        """按顶层目录把文件分组，并按目录名启发式归类到层。"""
        groups: dict[str, list[str]] = {}
        for path in self.files:
            key = _component_of(path)
            groups.setdefault(key, []).append(path)
        self._file_count = {key: len(items) for key, items in groups.items()}
        self.components = [(key, _classify_layer(key)) for key in sorted(groups)]

        seen: dict[str, tuple[str, str]] = {}
        for _, layer in self.components:
            if layer not in seen:
                seen[layer] = (_LAYER_PURPOSE.get(layer, ""), "由目录名推断，需人工确认")
        self.layers = [
            (name, purpose, hint) for name, (purpose, hint) in seen.items()
        ]

    def _read_targets(self) -> list[str]:
        """要读的文件：每个组件先取一个，再按列表补齐到上限。

        直接取列表前 N 个会让大目录吃掉全部名额 —— 靠后的组件一个文件都读不到，
        于是图上缺的不是「没有依赖」，而是「根本没看」。这种缺失看不出来，
        却会让整张图失真。
        """
        first_of_component: dict[str, str] = {}
        for path in self.files:
            first_of_component.setdefault(_component_of(path), path)
        picked = list(first_of_component.values())[: self.max_files]
        if len(picked) < self.max_files:
            chosen = set(picked)
            for path in self.files:
                if len(picked) >= self.max_files:
                    break
                if path not in chosen:
                    picked.append(path)
                    chosen.add(path)
        return picked

    def _collect_imports(self, path: str, text: str) -> None:
        """从**上下文里已读到的源码**解析 import，映射成组件之间的依赖。

        只认顶层包名且该包确实是已登记的组件：宁可少画一条边，
        也不要凭猜测画出一条并不存在的依赖。
        """
        source_lines, _ = _strip_line_numbers(text)
        key = _component_of(path)
        known = {name for name, _ in self.components}
        bucket = self.deps.setdefault(key, set())
        for line in source_lines:
            match = _IMPORT_RE.match(line)
            if not match:
                continue
            module = match.group(1) or match.group(2) or ""
            top = module.split(".")[0]
            if top in known and top != key:
                bucket.add(top)

    def _finish_design(self) -> Decision:
        total_deps = sum(len(items) for items in self.deps.values())
        return _call(
            ToolCall(
                "finish",
                {
                    "summary": (
                        f"离线规则模式盘点了 {len(self.files)} 个 Python 文件，"
                        f"按目录边界划出 {len(self.layers)} 层、{len(self.components)} 个模块，"
                        f"从 import 语句收集到 {total_deps} 条依赖。"
                        "\n\n这是**结构盘点**，不是设计方案：层与组件的划分来自目录名启发式，"
                        "依赖只覆盖顶层包名的静态 import（不含动态导入与运行时调用）。"
                        "是否应当这样切、哪些边界要拆或合并，需要真实模型基于代码语义判断 —— "
                        "已作为 blocking 的开放问题登记。"
                    )
                },
            ),
            reasoning="结构盘点完成，收尾。",
        )


# --------------------------------------------------------------------------- #
# 架构模式的辅助
# --------------------------------------------------------------------------- #

_LAYER_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("test", "spec", "bench"), "测试与评估"),
    (("api", "server", "web", "handler", "route", "view", "cli"), "接入层"),
    (("domain", "model", "core", "logic", "rule", "agent"), "领域层"),
    (("db", "storage", "sandbox", "repo", "dao", "infra", "tool"), "基础设施层"),
)

_LAYER_PURPOSE = {
    "测试与评估": "验证行为与度量能力，不参与运行时组装",
    "接入层": "协议转换与入口编排，不含业务规则",
    "领域层": "业务规则与状态变更",
    "基础设施层": "外部资源访问、进程隔离与工具能力",
    "支撑层": "不属于以上各层的辅助代码",
}


def _component_of(path: str) -> str:
    """文件 → 模块名：有顶层目录就用目录名，否则用文件名。"""
    head = path.replace("\\", "/").split("/")[0]
    return head.rsplit(".", 1)[0] if head.endswith(".py") else head


def _classify_layer(name: str) -> str:
    """按目录名启发式归类。这是推断，不是事实 —— 所以要写进 hint 里。"""
    lowered = name.lower()
    for keys, layer in _LAYER_RULES:
        if any(key in lowered for key in keys):
            return layer
    return "支撑层"


def _design_todos(stage: str, layers: int, read: int) -> list[dict[str, str]]:
    return [
        {"text": f"盘点现有结构（{layers} 层）", "status": "completed"},
        {"text": f"读取模块依赖（{read}）", "status": "in_progress"},
        {"text": f"登记组件与开放问题（当前阶段：{stage}）", "status": "pending"},
    ]


def _todos(files: list[str], done: int, findings: int) -> list[dict[str, str]]:
    return [
        {"text": f"枚举待审源文件（{len(files)} 个）", "status": "completed"},
        {
            "text": f"逐文件读取并检查（{done}/{len(files)}）",
            "status": "completed" if done >= len(files) else "in_progress",
        },
        {
            "text": f"记录发现（当前 {findings} 条）",
            "status": "in_progress" if done < len(files) else "completed",
        },
    ]


def _call(call: ToolCall, *, reasoning: str = "") -> Decision:
    return Decision(tool_calls=[call], content="", reasoning=reasoning)


def available_rule_ids() -> list[str]:
    return [rule.id for rule in RULES]
