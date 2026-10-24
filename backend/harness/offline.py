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

from .rules import scan_source
from .types import Decision, Message, ToolCall

_LINE_PREFIX = re.compile(r"^\s*\d+\s\|\s?(.*)$")
_FILE_ENTRY = re.compile(r"^(.+?)\s+\(\d+\s*B\)$")
# 只取「顶层包名」：它是把文件归到某个组件的依据
_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))")

AUDIT_AGENT = "review"
DESIGN_AGENT = "architect"
TEST_AGENT = "tester"
PLAN_AGENT = "planner"

def _last_tool_output(messages: Sequence[Message]) -> str:
    for message in reversed(messages):
        if message.role == "tool":
            return message.content
    return ""

#: 这些目录下的文件不是审查目标（配置、文档、锁文件）
_NON_SOURCE_DIRS = {"node_modules", ".git", "dist", "build", "vendor", "__pycache__"}


def _is_source_file(rel: str) -> bool:
    """是否已注册语言的源文件。

    离线模式用 ``list_files("*")`` 拉全量，再靠这个函数筛出代码文件 ——
    不筛的话会把 README、package.json、锁文件都读进来，浪费轮次预算。
    """
    from .languages import for_path

    p = rel.replace("\\", "/")
    if any(part in _NON_SOURCE_DIRS for part in p.split("/")[:-1]):
        return False
    return for_path(p) is not None


def _parse_file_list(text: str) -> list[str]:
    files: list[str] = []
    for line in text.splitlines():
        match = _FILE_ENTRY.match(line.strip())
        if match:
            files.append(match.group(1).strip())
    return files

_GAP_ENTRY = re.compile(r"^-\s+`([^`]+)`\s+\(([^:)]+):(\d+)")


def _parse_gap_list(text: str) -> list[tuple[str, str, int]]:
    """解析 check_coverage 输出里的「未覆盖符号」清单。

    格式：``- `name`  (file:line、file:line)``。
    只取第一个位置作为登记位置 —— 同名符号在多处定义时，
    取第一个足以让人找得到，精确到哪一个由人复核。
    """
    out: list[tuple[str, str, int]] = []
    for line in text.splitlines():
        match = _GAP_ENTRY.match(line.strip())
        if match:
            out.append((match.group(1), match.group(2).strip(), int(match.group(3))))
    return out


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
        # 测试模式的状态
        self.gaps: list[tuple[str, str, int]] = []
        self.reported_gap_index = 0

    # ------------------------------------------------------------------ #

    def complete(
        self,
        messages: Sequence[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> Decision:
        if self.agent == DESIGN_AGENT:
            return self._complete_design(messages)
        if self.agent == TEST_AGENT:
            return self._complete_test(messages)
        if self.agent == PLAN_AGENT:
            return self._complete_plan(messages)
        return self._complete_audit(messages)

    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #

    def _complete_plan(self, messages: Sequence[Message]) -> Decision:
        """策划助手的离线路径：盘点文件 → 按文件分组生成可验收的步骤。

        离线模式的步骤来自**确定性规则**（每个生产文件一步，改哪里=该文件，
        完成条件=该文件被改且原有测试全绿）。它当然比不上真实模型写的计划，
        但它保证了 `--agent planner --offline` 能跑通整条链，
        让 CLI 与前端在没有模型时也能演示计划的形状。
        """
        self.step += 1
        last = _last_tool_output(messages)

        # 1) 列计划
        if self.step == 1:
            return _call(
                ToolCall("update_todo", {
                    "todos": [
                        {"text": "盘点要改的范围", "status": "in_progress"},
                        {"text": "按依赖拆步骤并登记", "status": "pending"},
                        {"text": "登记前提并收尾", "status": "pending"},
                    ]
                }),
                reasoning="离线模式：先定范围，再按文件拆步骤。",
            )

        # 2) 枚举文件
        if self.step == 2:
            return _call(
                # 多语言：不能硬编码 "*.py"。用 `*` 然后由 _parse_file_list +
                # _is_source_file 过滤 —— 只认已注册语言的源文件。
                # 硬编码 "*.py" 会让离线模式在 JS/Go 仓库上一无所获，
                # 而输出看起来是「没问题」。实测踩过。
                ToolCall("list_files", {"path": ".", "pattern": "*", "limit": 60}),
                reasoning="确定改动范围。",
            )

        # 3) 解析并开始逐步登记
        if self.step == 3:
            files = [
                f for f in _parse_file_list(last)
                if not (f.rsplit("/", 1)[-1].startswith("test_") or "/tests/" in f)
            ][: self.max_files]
            self.files = files
            if not files:
                return _call(ToolCall("finish", {
                    "summary": "没有找到可改的 Python 文件，离线计划到此结束。"
                }), reasoning="没有目标。")
            return _call(ToolCall("update_todo", {
                "todos": [
                    {"text": f"盘点要改的范围（{len(files)} 个文件）", "status": "completed"},
                    {"text": "按依赖拆步骤并登记", "status": "in_progress"},
                    {"text": "登记前提并收尾", "status": "pending"},
                ]
            }), reasoning=f"{len(files)} 个文件。")

        # 4) 逐文件登记一步
        if self.reported_gap_index < len(self.files):
            idx = self.reported_gap_index
            rel = self.files[idx]
            self.reported_gap_index += 1
            return _call(
                ToolCall("record_plan_step", {
                    "order": idx + 1,
                    "title": f"改造 {rel}",
                    "done_when": (
                        f"{rel} 的改动完成，且 python -m pytest 全绿、ruff check 无新增告警"
                    ),
                    "touches": [rel],
                    "why": "该文件属于本次改动范围",
                    "verify": "python -m pytest -q",
                    "evidence": f"{rel}:1",
                }),
                reasoning=f"登记第 {idx + 1} 步。",
            )

        # 5) 登记前提
        if not self._todos_closed:
            self._todos_closed = True
            return _call(
                ToolCall("record_plan_risk", {
                    "assumption": "本次改动只涉及上述文件，没有其他模块引用它们的内部实现",
                    "if_wrong": "若有未列出的调用方，改动会造成静默不兼容；"
                                "需要用 impact 核对每个被改符号的反向调用图",
                    "blocking": False,
                }),
                reasoning="离线模式无法核对跨模块引用，如实登记为假设。",
            )

        # 6) 收尾
        return _call(ToolCall("finish", {
            "summary": (
                f"离线模式按文件拆出 {len(self.files)} 步，每步都给了完成条件与验证命令。"
                "局限：步骤是按文件机械划分的，**没有识别真实依赖顺序**；"
                "真实模型运行时会读代码后按依赖排序。"
            )
        }), reasoning="收尾。")

    def _complete_test(self, messages: Sequence[Message]) -> Decision:
        """测试助手的离线路径：扫描覆盖缺口 → 为未覆盖符号登记特征测试。

        为什么离线模式也要支持 tester：8 个靶场与回归测试都靠离线跑，
        缺这条路径的话，`--agent tester --offline` 会掉进审查分支去调
        record_finding —— 而测试助手的工具箱里根本没有这个工具，直接报错。
        分派必须显式，不能让「没写分支」变成「走错路」。
        """
        self.step += 1
        last = _last_tool_output(messages)

        # 1) 先列计划
        if self.step == 1:
            return _call(
                ToolCall(
                    "update_todo",
                    {
                        "todos": [
                            {"text": "扫描覆盖缺口", "status": "in_progress"},
                            {"text": "为未覆盖的行为登记特征测试", "status": "pending"},
                            {"text": "收尾汇总", "status": "pending"},
                        ]
                    },
                ),
                reasoning="离线模式：先机械扫描缺口，再按清单登记用例。",
            )

        # 2) 机械扫描覆盖缺口
        if self.step == 2:
            return _call(
                ToolCall("check_coverage", {"path": ".", "limit": 12}),
                reasoning="先拿一份客观的未覆盖清单，而不是凭感觉挑。",
            )

        # 3) 解析清单
        if self.step == 3:
            self.gaps = _parse_gap_list(last)
            if not self.gaps:
                return _call(
                    ToolCall("finish", {
                        "summary": "扫描后没有发现「完全未被测试引用」的公开符号，"
                                   "或仓库里没有公开符号。离线模式到此为止。",
                    }),
                    reasoning="没有可补的目标。",
                )
            return _call(
                ToolCall("update_todo", {
                    "todos": [
                        {"text": f"扫描覆盖缺口（{len(self.gaps)} 个未覆盖）", "status": "completed"},
                        {"text": "为未覆盖的行为登记特征测试", "status": "in_progress"},
                        {"text": "收尾汇总", "status": "pending"},
                    ]
                }),
                reasoning=f"找到 {len(self.gaps)} 个未覆盖符号。",
            )

        # 4) 逐个登记特征测试
        if self.step >= 4 and self.reported_gap_index < len(self.gaps):
            name, file, line = self.gaps[self.reported_gap_index]
            self.reported_gap_index += 1
            return _call(
                ToolCall("record_test_case", {
                    "target": name,
                    "file": file,
                    "line": line,
                    "title": f"锁定 {name} 的现有行为",
                    "kind": "characterization",
                    # should_fail_when 必须写具体条件 —— 这里写「行为被改动」是可判定的
                    "should_fail_when": (
                        f"`{name}` 的行为被改动且没有同步更新这条测试时"
                        "（包括返回值类型变化、分支条件变化、异常类型变化）"
                    ),
                    "pins_behavior": (
                        f"当前实现下 `{name}` 会按 {file} 里的现有逻辑返回结果；"
                        "离线模式未逐行推断具体返回值，运行时应读代码补全这一步"
                    ),
                    "rationale": f"`{name}` 是公开符号，但没有任何测试引用过它",
                    "evidence": f"{file}:{line}",
                }),
                reasoning=f"为未覆盖的 `{name}` 建立安全网。",
            )

        # 5) 收尾
        return _call(
            ToolCall("finish", {
                "summary": (
                    f"扫描到 {len(self.gaps)} 个完全未被测试引用的公开符号，"
                    f"已为其中 {min(self.reported_gap_index, len(self.gaps))} 个登记特征测试。"
                    "注意：特征测试建立的是安全网（改动会立刻显形），不代表代码正确。"
                    "离线模式未逐个读实现推断具体断言，实际运行时应当读代码补全。"
                )
            }),
            reasoning="覆盖缺口已登记，收尾。",
        )

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
                # 多语言：不能硬编码 "*.py"。用 `*` 然后由 _parse_file_list +
                # _is_source_file 过滤 —— 只认已注册语言的源文件。
                # 硬编码 "*.py" 会让离线模式在 JS/Go 仓库上一无所获，
                # 而输出看起来是「没问题」。实测踩过。
                ToolCall("list_files", {"path": ".", "pattern": "*", "limit": 60}),
                reasoning="枚举 Python 源文件。",
            )

        # 3) 解析文件列表
        if self.step == 3:
            self.files = [f for f in _parse_file_list(last) if _is_source_file(f)][: self.max_files]
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

