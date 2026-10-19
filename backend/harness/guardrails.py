"""护栏：约束 / 验证 / 纠正。

书中的关键判断：生产级 Harness 的绝大部分代码都在这三件事上，而不是在「给模型工具」上。
三条设计纪律在这里的具体体现：

- **约束**：故障安全默认值 —— 能力默认关闭，必须显式开放；高风险动作要额外审查。
- **验证**：只看**结构化**结果（工具返回、文件是否真的变了），不看模型自由生成的文本 ——
  后者可能已被提示注入操纵。
- **纠正**：连续失败要断电（熔断），不要把半成品当结果展示；拒绝理由要作为工具结果回传给模型，
  让它用自己的「工具失败」处理能力去消化，而不是简单重试同一次调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .tools import Risk, ToolRegistry
from .types import RunState, ToolCall, ToolResult

ApprovalFn = Callable[[ToolCall, Risk], tuple[bool, str]]

# 连续被拒熔断的豁免名单。
#
# 熔断的唯一目的是「别让模型把预算烧死在被禁止的操作上」，而不是切断它的全部能力。
# 这几类工具本身不改动外部环境（风险由参数校验与状态写入承担），一旦被熔断牵连：
#   - finish 被拒 → 循环无法正常收尾，只能撞到 max_iterations；
#   - record_* / update_todo 被拒 → 已经查实的产出全部丢失。
# 也就是把一次「纪律问题」放大成「结果丢失」。真实运行中已经踩到过：
# 模型连试几次高风险工具后，14 条发现一条都没能落库。
#
# record_* 用**前缀**而不是逐个列举：每加一个 agent 就会多出几个产出工具，
# 名单写死就得记得同步改，漏一个就是「熔断期间该类产出全部丢失」这种很难复现的 bug。
_EXEMPT_NAMES = frozenset({"finish", "update_todo"})
_RECORD_PREFIX = "record_"


def is_circuit_exempt(name: str) -> bool:
    """该工具是否不受熔断影响。"""
    return name in _EXEMPT_NAMES or name.startswith(_RECORD_PREFIX)


# =============================================================================
# 约束
# =============================================================================


@dataclass
class ConstraintDecision:
    allowed: bool
    reason: str = ""
    result: ToolResult | None = None


@dataclass
class Constrainer:
    """工具调用前的准入检查。"""

    registry: ToolRegistry
    repeat_limit: int = 3
    approve: ApprovalFn | None = None
    # 故障安全默认值：未显式放开时，高风险工具一律拒绝
    allow_high_risk: bool = False
    reject_streak_limit: int = 5

    def check(self, call: ToolCall, state: RunState) -> ConstraintDecision:
        tool = self.registry.get(call.name)
        if tool is None:
            # 交由 registry.execute 产出标准的 UnknownTool 结果
            return ConstraintDecision(allowed=True)

        # ---- 重复调用熔断 ----
        # 这是逐工具、可自愈的：参数一改，state.repeat_streak 就归零。
        signature = call.signature()
        if signature == state.last_signature and state.repeat_streak >= self.repeat_limit:
            return ConstraintDecision(
                allowed=False,
                reason="repeat",
                result=ToolResult.failure(
                    "RepeatedCall",
                    f"`{call.name}` 已连续用完全相同的参数调用了 {state.repeat_streak} 次。",
                    call.arguments,
                    suggestion="重复同样的调用不会产生新信息。请改变参数、换一个工具，"
                    "或基于已有证据直接给出结论。",
                ),
            )

        # ---- 连续被拒熔断 ----
        # 判据是 state.reject_streak（连续），不是 rejected_calls（累计）。
        # 早期版本用累计值且从不归零，于是熔断一旦打开就再也关不上，连 finish 都被拒 ——
        # 模型被自己的护栏锁死。豁免判据见 is_circuit_exempt（名单 + record_ 前缀）。
        #
        # 注意一个边界：同一次响应里的多个并行调用会连续累加计数，
        # 所以一批 5 个高风险调用就能打开熔断。这是可接受的 ——
        # 打开后只拦非豁免工具，模型仍能记录发现并正常收尾。
        if not is_circuit_exempt(call.name) and state.reject_streak >= self.reject_streak_limit:
            return ConstraintDecision(
                allowed=False,
                reason="reject_streak",
                result=ToolResult.failure(
                    "CircuitOpen",
                    f"已连续 {state.reject_streak} 次调用被拒绝，该工具箱暂时熔断。",
                    call.arguments,
                    suggestion="停止尝试被禁止的操作。用只读工具继续调查，"
                    "或直接用 record_finding 记录已确认的结论、用 finish 收尾。",
                ),
            )

        # ---- 风险分级审批 ----
        if tool.risk is Risk.HIGH and not self.allow_high_risk:
            if self.approve is not None:
                ok, note = self.approve(call, tool.risk)
                if ok:
                    return ConstraintDecision(allowed=True, reason=note)
                return ConstraintDecision(
                    allowed=False,
                    reason="approval_denied",
                    result=ToolResult.failure(
                        "ApprovalDenied",
                        f"高风险工具 `{call.name}` 的审批未通过：{note or '未说明理由'}",
                        call.arguments,
                        suggestion="改用低风险工具，或说明必要性后改用其他路径。",
                    ),
                )
            return ConstraintDecision(
                allowed=False,
                reason="high_risk_blocked",
                result=ToolResult.failure(
                    "HighRiskBlocked",
                    f"`{call.name}` 属于高风险工具，当前运行未开启高风险权限。",
                    call.arguments,
                    suggestion="避免使用它。审计通常可以用只读工具完成；"
                    "若确实需要执行命令，请让使用者显式放开权限。",
                ),
            )

        return ConstraintDecision(allowed=True)


# =============================================================================
# 验证
# =============================================================================


@dataclass
class Verification:
    """一次工具调用之后的核验结论。"""

    ok: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class Verifier:
    """自动核验工具是否真的产生了预期效果。

    刻意只依赖结构化事实：文件是否真的改了、内容是否还能解析。
    不读模型的自由文本 —— 那是提示注入最容易藏身的地方。
    """

    workspace: Path | None = None

    def verify(self, call: ToolCall, result: ToolResult, state: RunState) -> Verification:
        notes: list[str] = []

        # 写操作：确认文件真的变了，并且仍是合法文本
        if call.name == "apply_patch" and result.ok:
            path = str(call.arguments.get("path", ""))
            if path and self.workspace is not None:
                target = (self.workspace / path).resolve()
                if not target.is_file():
                    return Verification(
                        ok=False,
                        notes=[f"补丁声称成功，但目标文件不存在：{path}"],
                    )
                try:
                    target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    return Verification(
                        ok=False,
                        notes=[f"补丁写入后文件无法按 utf-8 读回：{exc}"],
                    )
                notes.append(f"已确认 {path} 可读回（{target.stat().st_size} 字节）")

        # 读文件：确认返回的内容里确实有行号（说明不是空文件/错误路径）
        if call.name == "read_file" and result.ok:
            body = str(result.data or "")
            if "共 0 行" in body or body.strip().endswith("0 行"):
                notes.append("目标文件为空。如果不是预期情况，请确认路径是否正确。")

        # 记录被读过的文件，供状态栏展示
        if call.name in {"read_file", "read_symbols", "lint_file"} and result.ok:
            path = str(call.arguments.get("path", ""))
            if path:
                state.files_read.add(path)

        return Verification(ok=True, notes=notes)


# =============================================================================
# 纠正
# =============================================================================


@dataclass
class Corrector:
    """失败熔断与降级。"""

    # 连续工具失败多少次就停止尝试工具，强制让模型下结论
    failure_limit: int = 5
    consecutive_failures: int = 0
    total_failures: int = 0
    history: list[str] = field(default_factory=list)

    def observe(self, call: ToolCall, result: ToolResult) -> str:
        """记录一次结果，返回需要注入到状态栏的提示（无则空串）。"""
        if result.ok:
            self.consecutive_failures = 0
            return ""

        self.consecutive_failures += 1
        self.total_failures += 1
        line = f"{call.name}: {result.error_type} - {result.message[:120]}"
        self.history.append(line)

        if self.consecutive_failures >= self.failure_limit:
            return (
                f"已连续 {self.consecutive_failures} 次工具调用失败。"
                "请停止继续尝试工具：要么换一个明确不同的方案，要么直接调用 finish "
                "说明卡在哪里、已经确认了什么。"
            )
        if self.consecutive_failures >= 2:
            return (
                f"已有 {self.consecutive_failures} 次连续失败。"
                "先读一遍错误里的 [建议] 字段，确认参数或路径哪里不对，再动手。"
            )
        return ""

    def summary(self) -> str:
        if not self.history:
            return "（无工具失败）"
        return "\n".join(f"- {line}" for line in self.history[-8:])


# =============================================================================
# 完成度核验（对标书里的「做对了但说错了」）
# =============================================================================


def check_completion_claims(state: RunState, final_text: str) -> list[str]:
    """检查模型声称的完成情况与它实际做的事情是否一致。

    书中 τ²-bench 的数据：704 次失败运行里有 162 次是「信息告知出错」，
    其中 80 次环境状态其实是对的 —— 做对了但说错了。这里做最基础的粗筛。
    """
    problems: list[str] = []

    # 泛化成「没有任何产出」：架构助手的产出是组件与决策，写死「发现数为 0」
    # 会让它在没产出时收到一条与自己无关的检查。
    if not any(state.artifacts.values()) and "没有" not in final_text and "未发现" not in final_text:
        problems.append("本次没有任何产出，但总结里没有说明「检查了哪些范围 / 为什么没有结论」")

    if state.todos:
        unfinished = [todo for todo in state.todos if todo.status not in {"completed", "cancelled"}]
        if unfinished and "未完成" not in final_text and "未覆盖" not in final_text:
            problems.append(
                f"任务列表仍有 {len(unfinished)} 项未完成，但总结里没有提到未覆盖的部分"
            )

    if state.rejected_calls and "拒绝" not in final_text and "受限" not in final_text:
        problems.append(f"有 {state.rejected_calls} 次调用被护栏拒绝，总结里没有说明")

    return problems


def extract_cited_locations(text: str) -> set[str]:
    """从自由文本里抽取 `file:line` 形式的引用，用于粗略核对是否编造位置。"""
    import re

    return set(re.findall(r"[\w./\\-]+\.[A-Za-z0-9_]+:\d+", text))


def verify_citations(text: str, state: RunState) -> list[str]:
    """核对结论里引用的位置是否为本次实际读过的文件。

    只做保守判断：如果引用的文件从未出现在 files_read 里，就提示一次 ——
    可能是编造的行号，也可能是通过 grep 看到的。

    注意：files_read 目前只由 read_file / read_symbols / lint_file 写入
    （grep 不写）。架构助手大量用 grep 建立结构认知，其总结里引自 grep 的
    ``file:line`` 会被这里提示「需人工复核」—— 那是**已知的误报**，用来稀释
    信号总好过把编造的行号一起放过。要消除它就给 grep 命中也记 files_read，
    但那会让「读过」与「逐行看过」混淆，暂时选择保守。
    """
    cited = extract_cited_locations(text)
    if not cited:
        return []

    known = {path for path in state.files_read}
    unknown: list[str] = []
    for item in cited:
        path = item.rsplit(":", 1)[0]
        normalized = path.replace("\\", "/").lstrip("./")
        if not any(normalized.endswith(known_path) or known_path.endswith(normalized) for known_path in known):
            unknown.append(item)
    if unknown:
        return [f"以下引用位置在本次运行中没有被读取过，需人工复核：{', '.join(sorted(unknown)[:8])}"]
    return []

# --------------------------------------------------------------------------- #
# 引用内容核验：不只看「文件读没读」，还看「那一行到底写了什么」
# --------------------------------------------------------------------------- #

# 判定用的关键词族。每条发现描述的是一条缺陷，描述里的技术词应当能在
# 所指的代码行（或它紧邻的上下文）里找到落点，找不到就有编造风险的嫌疑。
# 刻意用「族」而不是精确词：模型写 `shell` / `subprocess` / `注入` 都算同族。
_CUE_FAMILIES: dict[str, tuple[str, ...]] = {
    "shell": ("shell", "subprocess", "os.system", "popen", "命令注入", "注入", "eval", "exec"),
    "hardcoded": ("password", "secret", "token", "key", "credential", "hardcode", "硬编码", "口令", "密钥"),
    # 刻意不含裸 "query"/"拼接"：它们也出现在「查询」「字符串拼接」这类无关描述里，
    # 实测会把「auth.py:6 说 SQL 注入」这种错位判定放过。只保留 SQL 独有的强线索。
    "sql": ("sql", "execute(", "cursor", "%.format", "f\"select", "select *", "insert into", "where "),
    "assert": ("assert", "校验", "校验被剥离", "-o", "参数校验"),
    "resource": ("close", "release", "finally", "with ", "泄漏", "acquire", "open(", "__enter__"),
    "concurrency": ("lock", "thread", "async", "await", "race", "竞态", "锁", "共享", "global"),
    "boundary": ("len(", "range(", "index", "[0]", "[-1]", "边界", "越界", "空", "none"),
    "crypto": ("md5", "sha1", "hash", "salt", "aes", "random", "加密", "盐", "摘要", "tls", "verify"),
    "authz": ("user_id", "owner", "tenant", "permission", "role", "权限", "越权", "归属", "鉴权"),
    "error": ("except", "raise", "try", "error", "异常", "吞", "pass", "吞掉"),
    "default_arg": ("def ", "=[]", "={}", "默认参数", "mutable", "可变"),
    "path": ("path", "join", "open(", "..", "路径", "穿越"),
}

# 归一化时要剥掉的字符：模型描述里的标点不该影响匹配
_STRIP = "，。、：；！？（）()[]【】「」“”'\"`~,.;:!?"


def _normalized_tokens(text: str) -> str:
    """把描述与代码行都压成「只留字母数字与下划线的中文混排小写串」。

    两侧都归一化之后再判包含关系 —— 否则 `shell=True` 与 `shell = True`
    会被当成两个不同的东西。
    """
    import re

    return re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", "", (text or "").lower())


def _cue_families(text: str) -> set[str]:
    """描述命中了哪些关键词族。"""
    low = (text or "").lower()
    hit: set[str] = set()
    for family, cues in _CUE_FAMILIES.items():
        if any(cue.lower() in low for cue in cues):
            hit.add(family)
    return hit


def cross_check_line_content(
    finding: dict[str, Any],
    workspace_root: Path,
) -> tuple[bool, str]:
    """核对一条发现引用的行号，那一行是否真的在支撑这条描述。

    返回 ``(是否可信, 说明)``。这是**确定性检查**，不调用模型 ——
    业界（CRJudgeBench）实测最强 LLM 判断「看起来专业但其实是错的评论」
    召回只有 20%，所以在能确定性判定的地方就不该交给模型。

    判据（任一成立即视为可信）：
    1. 引用行里出现了描述中的关键词族线索；
    2. 引用行 ±2 行的上下文里有该线索（缺陷常写在多行表达式上）；
    3. 引用行本身是空行/注释/括号收尾 —— 这类「指向结构而非语句」的引用
       常见且无害，不做否定判定。

    行号超出文件长度（文件被改写或纯编造）直接判不可信。
    """
    import unicodedata

    rel = str(finding.get("file") or "").replace("\\", "/").lstrip("./")
    line_no = finding.get("line")
    description = " ".join(
        str(finding.get(k) or "") for k in ("title", "detail", "evidence", "suggestion")
    )
    if not rel or not isinstance(line_no, int) or line_no <= 0:
        return True, "未提供可核验的位置，跳过"

    try:
        target = (Path(workspace_root) / rel).resolve()
        # 不允许逃出工作区
        target.relative_to(Path(workspace_root).resolve())
    except (ValueError, OSError):
        return True, "路径无法解析，跳过"

    if not target.is_file():
        return False, f"文件不存在：{rel}"

    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return True, f"读取失败，跳过（{exc}）"

    if line_no > len(lines):
        return False, f"行号越界：{rel}:{line_no}（该文件只有 {len(lines)} 行）"

    actual = lines[line_no - 1]
    stripped = actual.strip()

    # 结构行（空行/注释/括号）不直接放行 —— 实测发现「乱指到空行」的假发现会因此漏过。
    # 改成继续走下面的线索核对：邻域里能对上才算可信，对不上就是错位引用。
    is_structural = not stripped or stripped.startswith("#") or stripped in ("}", ")", "]", "pass")

    window = "\n".join(lines[max(0, line_no - 3): line_no + 2])
    families = _cue_families(description)
    if not families:
        return True, "描述里没有可核对的技术线索，跳过"

    norm_desc = _normalized_tokens(description)
    norm_window = _normalized_tokens(window)

    # 判据 1/2：关键词族里的线索是否在引用行（或邻域）出现
    cue_hit = False
    low_window = window.lower()
    for family in families:
        for cue in _CUE_FAMILIES[family]:
            norm_cue = _normalized_tokens(cue)
            if norm_cue and norm_cue in norm_window:
                cue_hit = True
                break
        if cue_hit:
            break

    # 命中线索还要求「引用行本身有东西」：指向空行/注释时，靠邻域凑线索
    # 属于凑巧（实测 utils.py:20 这个空行就因为邻近的函数签名里有 path 而被放过）。
    if cue_hit and not is_structural:
        return True, "引用行与描述吻合"

    # 判据 3：引用行里是否有描述中出现的标识符（函数名/变量名）
    import re

    identifiers = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", description))
    tokens_here = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", actual))
    shared = {i.lower() for i in identifiers} & {t.lower() for t in tokens_here}
    shared -= {"the", "and", "for", "with", "this", "that", "from", "not", "none", "true", "false"}
    if shared:
        return True, f"引用行含描述提到的标识符：{sorted(shared)[:3]}"

    if is_structural:
        return False, f"引用位置指向空行/注释等结构行，与描述无关（{rel}:{line_no}）"

    snippet = stripped[:60]
    return False, f"引用行内容与描述无关（{rel}:{line_no} 实际是 `{snippet}`）"


def verify_finding_locations(
    findings: Sequence[dict[str, Any]],
    workspace_root: Path,
) -> list[str]:
    """批量核验发现的位置真实性，返回需要人工复核的提示。

    单独成函数（而不是塞进 verify_citations）的用意：它可以直接被判官层
    复用，也可以在 eval 里对**历史结果**离线复核，不依赖一次运行的状态。
    """
    warnings: list[str] = []
    ungrounded: list[str] = []
    for finding in findings:
        ok, why = cross_check_line_content(finding, workspace_root)
        if not ok:
            label = f"{finding.get('file')}:{finding.get('line')} {(finding.get('title') or '')[:40]}"
            ungrounded.append(f"{label} —— {why}")
    if ungrounded:
        warnings.append(
            f"{len(ungrounded)} 条发现的位置与描述对不上（行号可能编造或已过期），需人工复核："
            + "；".join(ungrounded[:5])
        )
    return warnings

