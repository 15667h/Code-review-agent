"""数据结构。

对应书中「Agent = LLM + 上下文 + 工具」在 API 层的映射：
一次请求 = ``messages`` 列表（system / user / assistant / tool）+ 顶层 ``tools`` 字段。
模型 API 是无状态的，每轮都由 Harness 重新构造完整上下文。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, fields
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
Severity = Literal["blocker", "warn", "note"]
TodoStatus = Literal["pending", "in_progress", "completed", "cancelled"]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# =============================================================================
# 工具调用
# =============================================================================


@dataclass(slots=True)
class ToolCall:
    """模型发起的一次工具调用。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: _new_id("call"))

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ToolCall:
        fn = raw.get("function") or {}
        args = fn.get("arguments") or "{}"
        parsed: dict[str, Any]
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except json.JSONDecodeError:
                # 参数不是合法 JSON：原样保留，交给 guardrails.constrain 拒绝，
                # 而不是在这里悄悄“修好”它（书中禁止静默输入转换）。
                parsed = {"__unparsed__": args}
        elif isinstance(args, dict):
            parsed = args
        else:
            parsed = {"__unparsed__": str(args)}

        return cls(name=fn.get("name") or "", arguments=parsed, id=raw.get("id") or _new_id("call"))

    def signature(self) -> str:
        """用于重复调用检测：同名同参视为一次重复。"""
        return f"{self.name}:{json.dumps(self.arguments, sort_keys=True, ensure_ascii=False)}"


# =============================================================================
# 工具返回
# =============================================================================


@dataclass(slots=True)
class ToolResult:
    """工具的统一返回信封。

    书中的实验结论：把错误信息写全（错误类型 + 完整参数 + 调用栈 + 修复建议），
    模型找到替代方案的成功率从 60% 提升到 95%。所以错误路径也必须有信息量。
    """

    ok: bool
    data: Any = None
    error_type: str | None = None
    message: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""
    traceback: str | None = None
    truncated: bool = False
    saved_to: str | None = None

    def render(self) -> str:
        """渲染成给模型看的文本。成功时只给数据，失败时给完整的四层信息。"""
        if self.ok:
            body = self.data if isinstance(self.data, str) else json.dumps(
                self.data, ensure_ascii=False, indent=2, default=str
            )
            if self.truncated and self.saved_to:
                body += f"\n\n[输出已截断，完整内容见 {self.saved_to}]"
            return body

        lines = [
            f"[错误] {self.error_type}: {self.message}",
            f"[参数] {json.dumps(self.params, ensure_ascii=False, default=str)}",
        ]
        if self.traceback:
            lines.append(f"[调用栈]\n{self.traceback}")
        if self.suggestion:
            lines.append(f"[建议] {self.suggestion}")
        return "\n".join(lines)

    @classmethod
    def success(cls, data: Any = None, **kwargs: Any) -> ToolResult:
        return cls(ok=True, data=data, **kwargs)

    @classmethod
    def failure(
        cls,
        error_type: str,
        message: str,
        params: dict[str, Any] | None = None,
        suggestion: str = "",
        traceback: str | None = None,
    ) -> ToolResult:
        return cls(
            ok=False,
            error_type=error_type,
            message=message,
            params=params or {},
            suggestion=suggestion,
            traceback=traceback,
        )


# =============================================================================
# 消息
# =============================================================================


@dataclass(slots=True)
class Message:
    """一条上下文消息。"""

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # 压缩标记：书中的防重复保护，已压缩内容永不再压
    compressed: bool = False
    # 压缩前的内容，便于审计与回溯（不进上下文）
    raw_content: str | None = None

    def to_api(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [call.to_api() for call in self.tool_calls]
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.role == "tool" and self.name:
            payload["name"] = self.name
        return payload



# =============================================================================
# 模型输出与用量
# =============================================================================


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass(slots=True)
class Decision:
    """模型的一次响应。"""

    reasoning: str = ""
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""

    @property
    def is_final(self) -> bool:
        """没有任何工具调用即视为给出最终答复。"""
        return not self.tool_calls

    def to_message(self) -> Message:
        return Message(role="assistant", content=self.content, tool_calls=list(self.tool_calls))


# =============================================================================
# 审计域
# =============================================================================


@dataclass(slots=True)
class Finding:
    """一条审计发现。"""

    severity: Severity
    file: str
    line: int
    title: str
    detail: str
    evidence: str
    suggestion: str = ""
    id: str = field(default_factory=lambda: _new_id("F"))
    # 用于去重：同一文件同一行同一标题只保留一条
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = f"{self.file}:{self.line}:{self.title}".lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
        }


# =============================================================================
# 设计域（架构助手的产出）
# =============================================================================
#
# 与「发现」是并列关系，不是它的变体。审查回答「哪里坏了」，
# 架构回答「该怎么切、为什么、还有什么没定」 —— 后者没有严重级别，
# 也没有单一的正确位置，所以数据结构从一开始就不一样。


@dataclass(slots=True)
class Layer:
    """架构分层。层与层之间的依赖方向是设计意图的直接体现。"""

    name: str
    purpose: str = ""
    hint: str = ""
    order: int = 0
    id: str = field(default_factory=lambda: _new_id("L"))
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = f"layer:{self.name}".lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "purpose": self.purpose,
            "hint": self.hint,
            "order": self.order,
        }


@dataclass(slots=True)
class Component:
    """一个组件（服务 / 模块 / 存储）。依赖写在 depends_on 里，由前端据此画连线。"""

    name: str
    layer: str = ""
    role: str = ""
    responsibility: str = ""
    interfaces: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    failure_modes: list[str] = field(default_factory=list)
    rationale: str = ""
    id: str = field(default_factory=lambda: _new_id("C"))
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = f"component:{self.name}".lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "layer": self.layer,
            "role": self.role,
            "responsibility": self.responsibility,
            "interfaces": list(self.interfaces),
            "depends_on": list(self.depends_on),
            "failure_modes": list(self.failure_modes),
            "rationale": self.rationale,
        }


@dataclass(slots=True)
class DecisionRecord:
    """一条设计决策。写清「放弃了什么」比写清「选了什么」更有价值。"""

    title: str
    decision: str = ""
    rationale: str = ""
    alternatives: list[str] = field(default_factory=list)
    consequences: str = ""
    id: str = field(default_factory=lambda: _new_id("D"))
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = f"decision:{self.title}".lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "decision": self.decision,
            "rationale": self.rationale,
            "alternatives": list(self.alternatives),
            "consequences": self.consequences,
        }


@dataclass(slots=True)
class OpenQuestion:
    """一个尚未确定的问题。

    架构助手必须能说「这里我没定」 —— 一个把所有不确定都抹平的方案
    看起来完整，实际上把风险藏起来了。
    """

    question: str
    why_it_matters: str = ""
    blocking: bool = False
    id: str = field(default_factory=lambda: _new_id("Q"))
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = f"question:{self.question}".lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "why_it_matters": self.why_it_matters,
            "blocking": self.blocking,
        }


@dataclass(slots=True)
class Todo:
    text: str
    status: TodoStatus = "pending"
    id: str = field(default_factory=lambda: _new_id("T"))

    def to_line(self) -> str:
        mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "cancelled": "[-]"}[
            self.status
        ]
        return f"{mark} {self.id} {self.text}"


@dataclass(slots=True)
class RunState:
    """运行状态。状态栏就是它的渲染结果，由代码维护、不交给模型统计。"""

    goal: str = ""
    iteration: int = 0
    max_iterations: int = 40
    todos: list[Todo] = field(default_factory=list)
    # 产出按类型分桶。审查只有 finding，架构有 layer/component/decision/open_question。
    # 用字典而不是给每个 agent 加字段，是因为「边录边推、去重、落库、序列化」这条管道
    # 对所有 agent 都一样，只有桶的名字和里面装的类型不同。
    artifacts: dict[str, list[Any]] = field(default_factory=dict)
    tool_counts: dict[str, int] = field(default_factory=dict)
    repeat_streak: int = 0
    last_signature: str = ""
    usage: Usage = field(default_factory=Usage)
    files_read: set[str] = field(default_factory=set)
    # 累计被护栏拒绝次数（只用于运行后的汇报）
    rejected_calls: int = 0
    # **连续**被护栏拒绝次数（熔断判据）。由 loop 在放行正常工具时归零。
    reject_streak: int = 0
    stop_reason: str = ""

    # ---- 统计辅助（全部由代码维护） ----

    def note_tool(self, signature: str) -> None:
        name = signature.split(":", 1)[0]
        self.tool_counts[name] = self.tool_counts.get(name, 0) + 1
        if signature == self.last_signature:
            self.repeat_streak += 1
        else:
            self.repeat_streak = 1
            self.last_signature = signature

    # ---- 产出 ---- #

    @property
    def findings(self) -> list[Finding]:
        """审查产出的视图，指向 ``artifacts["finding"]``。

        保留它是为了让既有代码（报告、护栏、评估）不必改；
        新增 agent 请用 :meth:`add_artifact` 与 :meth:`artifacts_of`。

        只读：不能用 ``setdefault`` —— 那样只是「读一眼有没有发现」也会凭空建出
        一个空的 finding 桶，架构运行的结果里就会多出一条 ``finding: 0``。
        """
        return self.artifacts.get("finding", [])

    def bucket(self, kind: str) -> list[Any]:
        """取某个类型的产出桶（不存在就建一个，保证键顺序稳定）。"""
        return self.artifacts.setdefault(kind, [])

    def artifacts_of(self, kind: str) -> list[Any]:
        """只读地取某个类型的产出桶。"""
        return self.artifacts.get(kind, [])

    def add_artifact(self, kind: str, item: Any) -> bool:
        """登记一条产出。返回 True 表示是新条目，False 表示重复被丢弃。

        去重依赖条目上的 ``fingerprint``；没有该属性的条目不去重。
        """
        bucket = self.bucket(kind)
        fingerprint = getattr(item, "fingerprint", None)
        if fingerprint:
            if any(getattr(existing, "fingerprint", None) == fingerprint for existing in bucket):
                return False
        bucket.append(item)
        return True

    def add_finding(self, finding: Finding) -> bool:
        """返回 True 表示是新发现，False 表示与已有发现重复。"""
        return self.add_artifact("finding", finding)

    def artifact_counts(self) -> dict[str, int]:
        return {kind: len(items) for kind, items in self.artifacts.items()}

    def severity_counts(self) -> dict[str, int]:
        counts = {"blocker": 0, "warn": 0, "note": 0}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts


# =============================================================================
# 产出的序列化往返
# =============================================================================
#
# 运行结束后，终态以 JSON 落盘；之后回放报告时要把它还原回对象。
# 只还原 finding 会让架构报告渲染出「本次没有登记分层」——
# 那不是事实，只是回放路径丢了一半产出。

ARTIFACT_TYPES: dict[str, type] = {
    "finding": Finding,
    "layer": Layer,
    "component": Component,
    "decision": DecisionRecord,
    "open_question": OpenQuestion,
}


def revive_artifact(kind: str, data: dict[str, Any]) -> Any:
    """把一条产出从字典还原成对象。

    未知类型原样返回字典：宁可让渲染器拿到一个 dict，
    也不要因为不认识这个 kind 就把整份产出丢掉。
    """
    cls = ARTIFACT_TYPES.get(kind)
    if cls is None:
        return dict(data)
    known = {field.name for field in fields(cls)}
    return cls(**{key: value for key, value in data.items() if key in known})


def revive_artifacts(raw: dict[str, list[dict[str, Any]]]) -> dict[str, list[Any]]:
    return {
        kind: [revive_artifact(kind, item) for item in items]
        for kind, items in (raw or {}).items()
    }
