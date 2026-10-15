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
    """用规则驱动工具链的离线模型。"""

    name = "heuristic-offline"

    def __init__(self, *, max_files: int = 12) -> None:
        self.max_files = max_files
        self.step = 0
        self.files: list[str] = []
        self._current_pair = 0
        self.reported: set[str] = set()

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
