"""测试助手工具集。

本文件只放**测试特有**的工具（record_test_case / check_coverage / run_generated_tests）。
公共的读写检索工具在 ``tools_common``，代码图查询在 ``tools_graph``。

## 这个助手的产出为什么不是「发现」

审查助手产出「哪里坏了」；测试助手产出「**哪些行为还没被锁住**」。
两者视角不同：
- 审查是**缺陷视角** —— 找已经存在的错误。
- 测试是**覆盖视角** —— 找「将来可能被改坏、但没人会发现」的地方。

一个没有任何已知缺陷的模块，照样可能一条测试都没有。那不是一个「缺陷」，
但它确实是风险 —— 只是风险的类型不同，所以产出类型也不同。

## 为什么 ``should_fail_when`` 是必填

这是本助手最重要的一个约束。它逼着作者回答「这条测试在什么情况下会失败」。

回答不出来，说明这条测试**没有断言任何东西** —— 那样的测试即使全绿，
也不构成任何证据。行业里有个说法很到位：「一条测试通过，
只有在原始缺陷代码上会失败时，才算关于该缺陷的证据。」

对特征测试尤其如此：它锁的是「当前实际行为」。如果它锁的是错误行为，
那么它会**固化缺陷** —— 修好之后测试反而失败。所以必须让人看见
「这条测试锁的到底是什么、什么情况下它该红」。
"""

from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path

from .sandbox import CommandRunner, PathEscape, Workspace
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
from .types import RunState, TEST_KINDS, ToolResult, TestCase

# =============================================================================
# 工具：登记测试用例
# =============================================================================


def _too_similar(a: str, b: str) -> bool:
    """判断两段文字是否「实质上是同一句话」。

    用 difflib 的相似度：0.85 以上视为重复。
    为什么要判这个：``should_fail_when`` 很容易被填成 title 的同义复述
    （「两数相加返回和」→「相加结果不对时」），那样这个字段就没有信息量了。
    """
    x = re.sub(r"\s+", "", (a or "").lower())
    y = re.sub(r"\s+", "", (b or "").lower())
    if not x or not y:
        return False
    return difflib.SequenceMatcher(None, x, y).ratio() >= 0.85


def _tool_record_test_case(state: RunState) -> Tool:
    def handler(
        target: str,
        file: str,
        line: int,
        title: str,
        kind: str,
        should_fail_when: str,
        pins_behavior: str = "",
        code: str = "",
        rationale: str = "",
        evidence: str = "",
        verified: str = "",
        verify_output: str = "",
    ) -> ToolResult:
        kind_norm = (kind or "").strip().lower()
        if kind_norm not in TEST_KINDS:
            return ToolResult.failure(
                "InvalidKind",
                f"kind 必须是 {list(TEST_KINDS)} 之一，收到 `{kind}`",
                {"kind": kind},
                suggestion="characterization 锁定当前实际行为；unit 锁定预期输出；"
                           "integration 锁定协作行为；regression 锁定修好的具体缺陷。",
            )

        # ---- 核心校验：should_fail_when 必须有信息量 ----
        fail_when = (should_fail_when or "").strip()
        if len(fail_when) < 8:
            return ToolResult.failure(
                "VagueShouldFailWhen",
                f"should_fail_when 太短（{len(fail_when)} 字），没有说明什么条件下的失败。",
                {"should_fail_when": should_fail_when},
                suggestion="写成可判定的条件，例如「被除数传 0 时抛出 ZeroDivisionError」"
                           "或「单价为负数时总额变成负数」。",
            )
        if _too_similar(fail_when, title):
            return ToolResult.failure(
                "VagueShouldFailWhen",
                "should_fail_when 与 title 几乎是同一句话，说明它没有回答「什么情况下会失败」。",
                {"title": title, "should_fail_when": should_fail_when},
                suggestion="title 说「锁住什么行为」，should_fail_when 说「什么具体条件下它会红」。"
                           "例如 title=「退款金额按实收而非应付」，"
                           "should_fail_when=「pay 传 0 而 refund 传全额时仍然退款成功」。",
            )

        # ---- characterization 必须写清锁的是哪个当前行为 ----
        if kind_norm == "characterization" and not (pins_behavior or "").strip():
            return ToolResult.failure(
                "MissingPinnedBehavior",
                "特征测试必须填 pins_behavior：它锁定的是「当前实际行为」，得说清是哪一个。",
                {"target": target},
                suggestion="写清楚现在实际会发生什么，哪怕你觉得那个行为是错的 —— "
                           "特征测试的价值正是把这个事实钉住。",
            )

        # ---- 验证状态：有代码却没跑过，必须如实标 unverified ----
        status = (verified or "").strip().lower()
        if status not in ("verified", "failed", "unverified"):
            # 没填时按有没有代码推断：有代码但没声明跑过 → 仍然是 unverified
            status = "unverified"

        # ---- 一致性提示：有代码却没声明验证，明确提醒（但不自动改）----
        #
        # 为什么不自动把 unverified 改成 verified：那是「替模型判断它跑过了」，
        # 而我们没有任何执行记录能证明这件事。项目纪律是「能确定性判定的不交给模型」，
        # 反面同样成立 —— 不能确定的事不要假装确定。所以只提醒，让它自己补。
        warning = ""
        if (code or "").strip() and status == "unverified":
            warning = (
                "\n\n[提示] 这条用例带可运行代码，但验证状态是 unverified。"
                "如果你已经用 run_generated_tests 跑过，请带上 verified=\"verified\" "
                "与 verify_output 重新登记（会覆盖这条）；如果没跑，保持现状是对的。"
            )

        case = TestCase(
            target=target,
            file=file,
            line=line,
            title=title,
            kind=kind_norm,
            should_fail_when=fail_when,
            pins_behavior=pins_behavior,
            code=code,
            rationale=rationale,
            evidence=evidence,
            verified=status,
            verify_output=verify_output,
        )
        # ---- 去重策略：重复时**更新验证状态**，而不是简单丢弃 ----
        #
        # 之前直接 add_artifact 会丢弃重复项。后果很具体：模型先登记一条
        # （还没跑，unverified），跑通后想补 verified 重新登记 —— 被当成重复丢掉，
        # 验证状态永远补不上去，报告永远显示「未验证」。
        # 这是实测里 12 条用例全停在 unverified 的直接原因之一。
        #
        # 正确语义：同一条用例（目标+标题相同）只有一个事实，
        # 后来的登记应当**覆盖**可观测字段（验证状态、代码、输出），而不是新增一条。
        bucket = state.artifacts_of("test_case")
        existing = next((c for c in bucket if c.fingerprint == case.fingerprint), None)
        if existing is not None:
            existing.code = code or existing.code
            existing.verify_output = verify_output or existing.verify_output
            # 验证状态只允许「从未验证 → 已验证」这个方向的更新
            if status in ("verified", "failed"):
                existing.verified = status
            existing.rationale = rationale or existing.rationale
            is_new = False
        else:
            bucket.append(case)
            is_new = True

        total = len(state.artifacts_of("test_case"))
        by_kind: dict[str, int] = {}
        for item in state.artifacts_of("test_case"):
            by_kind[getattr(item, "kind", "?")] = by_kind.get(getattr(item, "kind", "?"), 0) + 1
        summary = f"当前共 {total} 条测试用例（" + " / ".join(
            f"{k}={v}" for k, v in sorted(by_kind.items())) + "）"
        if is_new:
            return ToolResult.success(f"已记录 {case.id}：{title}\n{summary}{warning}")
        return ToolResult.success(
            f"该用例已存在（目标+行为重复），已更新其验证状态为 `{status}`，未新增。\n{summary}{warning}"
        )

    return Tool(
        name="record_test_case",
        summary="登记一条测试用例（target/title/kind/should_fail_when 必填）",
        description="""把一条准备好的测试用例登记进产出。

何时用：
- 你已经读过被测代码，能说清「这条测试锁住什么行为」和「什么情况下它会失败」。

何时不用：
- 还没读代码，只是猜「这里应该测一下」——先读代码，再回来登记。
- 想报告一个代码缺陷 → 那是审查助手的活儿，不是测试用例。

边界：
- **`should_fail_when` 必须回答「什么具体条件下这条测试会红」**，不能是 title 的同义复述。
  校验会拒绝：太短（<8 字）或与 title 相似度 ≥0.85 的内容。
  这个字段是刻意的约束 —— 回答不出来，说明这条测试没有断言任何东西。
- `kind=characterization` 时**必须**填 `pins_behavior`：说明锁的是哪个当前实际行为。
- `code` 是可选的：给了就能直接跑，不给就是一份可执行的规格说明。
- 同一 `target` + `title` 不会重复记录。

Returns: 记录结果与当前累计条数（按 kind 分组）。""",
        parameters={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "被测符号名，例如 `Workspace.resolve` 或 `refund`",
                },
                "file": {"type": "string", "description": "被测代码的相对路径"},
                "line": {"type": "integer", "description": "被测符号所在行号", "minimum": 1},
                "title": {
                    "type": "string",
                    "description": "这条测试锁住什么行为，一句话。例如「退款金额按实收而非应付计算」",
                },
                "kind": {
                    "type": "string",
                    "enum": list(TEST_KINDS),
                    "description": "characterization 锁当前实际行为 / unit 锁预期输出 / "
                                   "integration 锁协作 / regression 锁修好的缺陷",
                },
                "should_fail_when": {
                    "type": "string",
                    "description": "**必填且必须是具体条件**：什么情况下这条测试会失败。"
                                   "例如「pay=0 而 refund 传全额时仍然退款成功」",
                },
                "pins_behavior": {
                    "type": "string",
                    "description": "characterization 时必填：现在实际会发生什么（哪怕看着是错的）",
                },
                "code": {"type": "string", "description": "测试代码（可选）"},
                "rationale": {"type": "string", "description": "为什么需要这条测试（覆盖缺口在哪）"},
                "evidence": {
                    "type": "string",
                    "description": "读了哪处代码得出这个结论，格式 `file:line`",
                },
                "verified": {
                    "type": "string",
                    "enum": ["verified", "failed", "unverified"],
                    "description": "验证状态：用 run_generated_tests 真跑通过填 verified；"
                                   "跑过但没过填 failed；只有描述没跑过填 unverified（默认）。"
                                   "**只在 summary 里说「我跑了」不算 —— 必须填进这个字段。**",
                },
                "verify_output": {
                    "type": "string",
                    "description": "跑测试时拿到的关键输出（几行即可，例如 `13 passed in 0.21s`）",
                },
            },
            "required": ["target", "file", "line", "title", "kind", "should_fail_when"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[
            {
                "target": "refund",
                "file": "app/service.py",
                "line": 181,
                "title": "退款金额按实收金额而非应付金额计算",
                "kind": "characterization",
                "should_fail_when": "pay=0（未付款）而 refund 传全额时仍然退款成功",
                "pins_behavior": "当前实现用 order.total 而非 order.paid 计算可退金额，"
                                 "因此未付款订单也能全额退款",
                "rationale": "这个分支没有任何测试覆盖，而它直接决定能退多少钱",
                "evidence": "app/service.py:181",
            }
        ],
        keywords=("test", "case", "record", "测试", "用例", "登记", "覆盖"),
    )


# =============================================================================
# 工具：覆盖缺口扫描
# =============================================================================


def _tool_check_coverage(workspace: Workspace, state: RunState) -> Tool:
    """机械扫描：哪些公开函数完全没有测试引用。

    这是**确定性**的：只靠 AST 找「被测试文件引用过的符号」，不调用模型。
    为什么要有这个：模型会倾向于凭感觉挑几个重要函数来测，
    而这个工具能给出一份客观的「没被任何测试碰过的清单」，
    让模型的判断有个可核对的起点。
    """

    def handler(path: str = ".", limit: int = 40) -> ToolResult:
        try:
            target = workspace.resolve(path)
        except PathEscape as exc:
            return ToolResult.failure("PathEscape", str(exc), {"path": path},
                                      suggestion="只能用工作区内的相对路径。")
        if not target.exists():
            return ToolResult.failure("PathNotFound", f"路径不存在：{path}", {"path": path},
                                      suggestion="先 list_files('.') 看目录结构。")

        # ① 收集生产代码里的公开符号（跳过下划线开头与测试文件）
        #
        # 多语言：Python 走 AST（精确），其他语言走 ``_shallow_symbols``
        # 同一套启发式（按语言关键字定位）。**必须标注能力差异** ——
        # 把启发式结果和 AST 结果混在一起统计，会让人以为覆盖率数字是同一口径。
        prod: dict[str, list[tuple[str, int]]] = {}
        py_files = sorted(target.rglob("*.py"))
        other_files: list[Path] = []
        from .languages import LEVEL_DEEP, for_path as _for_path, all_suffixes

        for _suf in all_suffixes():
            if _suf == ".py":
                continue
            for _p in target.rglob(f"*{_suf}"):
                if _for_path(_p.name) and _for_path(_p.name).level != LEVEL_DEEP:
                    other_files.append(_p)

        for p in py_files:
            rel = workspace.relative(p)
            if _is_test_path(rel):
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("_"):
                        prod.setdefault(node.name, []).append((rel, node.lineno))
                elif isinstance(node, ast.ClassDef):
                    if not node.name.startswith("_"):
                        prod.setdefault(node.name, []).append((rel, node.lineno))
                    # 类里的公开方法也算
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if not sub.name.startswith("_") and sub.name not in ("__init__",):
                                prod.setdefault(sub.name, []).append((rel, sub.lineno))

        # 非 Python：用浅层扫描补一部分符号（只给「未覆盖」提示，不计入精确统计）
        shallow_found: dict[str, list[tuple[str, int]]] = {}
        for p in other_files[:200]:
            rel = workspace.relative(p)
            if _is_test_path(rel):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            from .rules import _iter_lines
            from .languages import for_path as _fp

            lang = _fp(rel)
            if not lang:
                continue
            for lineno, line in _iter_lines(text, lang.id):
                stripped = line.strip()
                if not stripped:
                    continue
                if any(kw in stripped for kw in lang.class_keywords + lang.func_keywords):
                    # 取一个粗略的名字：第一个标识符
                    import re as _re

                    m = _re.search(r"[A-Za-z_]\w*", stripped.split("(", 1)[0].split()[-1]
                                   if "(" in stripped else stripped.split()[-1])
                    if m and not m.group(0).startswith("_"):
                        shallow_found.setdefault(m.group(0), []).append((rel, lineno))

        if not prod and not shallow_found:
            return ToolResult.success(
                f"`{path}` 下没有找到公开的函数或类（或都是下划线开头的内部实现）。"
            )

        # ② 收集测试文件里出现过的名字（粗粒度：被 import 或被调用）
        tested: set[str] = set()
        test_files: list[str] = []
        for p in sorted(workspace.root.rglob("*.py")):
            rel = workspace.relative(p)
            if not _is_test_path(rel):
                continue
            test_files.append(rel)
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    tested.add(node.id)
                elif isinstance(node, ast.Attribute):
                    tested.add(node.attr)
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        tested.add(alias.name)

        gaps = [(name, locs) for name, locs in prod.items() if name not in tested]
        gaps.sort(key=lambda kv: (-len(kv[1]), kv[0]))

        # 浅层语言单独统计：口径不同，不能混进上面的数字
        shallow_gaps = [
            (name, locs) for name, locs in shallow_found.items() if name not in tested
        ]

        # 浅层语言单独统计：口径不同，不能混进上面的数字
        shallow_gaps = [
            (name, locs) for name, locs in shallow_found.items() if name not in tested
        ]

        lines = [
            f"扫描范围：`{path}`",
            f"生产代码里的公开符号：{len(prod)} 个",
            f"测试文件：{len(test_files)} 个",
            f"**没有被任何测试引用过的符号：{len(gaps)} 个**",
        ]
        if not test_files:
            lines.append("")
            lines.append("注意：这个仓库**一个测试文件都没有**。"
                         "所以这里的「全部未覆盖」是字面意义 —— 所有公开符号都没有测试。"
                         "这种情况下特征测试的价值最大：先把**当前实际行为**钉住，再谈改进。")

        if gaps:
            lines.append("")
            for name, locs in gaps[:limit]:
                where = "、".join(f"{f}:{ln}" for f, ln in locs[:3])
                lines.append(f"- `{name}`  ({where})")
            if len(gaps) > limit:
                lines.append(f"[... 还有 {len(gaps) - limit} 个，调大 limit 或缩小 path]")

        if shallow_gaps:
            lines.append("")
            lines.append(f"### 其他语言（启发式，口径不同，共 {len(shallow_gaps)} 个疑似未覆盖）")
            lines.append(
                "这些是**按关键字猜测**的名字（没有 AST），可能有误报；"
                "逐个核对成本高，建议只在低覆盖模块上参考。"
            )
            for name, locs in shallow_gaps[:limit]:
                where = "、".join(f"{f}:{ln}" for f, ln in locs[:2])
                lines.append(f"- `{name}`  ({where})")

        lines.append("")
        lines.append("**这是粗粒度扫描**：只判断「名字有没有在测试里出现过」，"
                     "不代表出现过就等于测好了。它给的是「先看哪些」的排序起点，不是覆盖率报告。")
        return ToolResult.success("\n".join(lines))

    return Tool(
        name="check_coverage",
        summary="机械扫描哪些公开符号完全没有被测试引用过",
        description="""列出「没有被任何测试文件引用过」的公开函数与类。

何时用：
- 开始规划测试时，先拿一份客观的覆盖缺口清单，而不是凭感觉挑。
- 想快速判断一个仓库有没有测试底子。

何时不用：
- 想知道某个具体符号被哪些测试覆盖 → 用 tests_for（更精确，需要代码图）。
- 想要真实的覆盖率百分比 → 本工具不跑测试，给不出行覆盖率。

边界：
- **粗粒度**：只判断「名字有没有在测试文件里出现过」。出现过 ≠ 测得好。
- 只统计**公开**符号（跳过 `_` 开头的内部实现与 `__init__`）。
- 不跑测试、不算行覆盖率，所以结果是「引用缺口」而不是「覆盖率」。
- 需要工作区可读，不需要建代码图。

Returns: 生产符号数、测试文件数、未覆盖符号清单（带文件与行号）。""",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "扫描范围，默认整个工作区 '.'"},
                "limit": {"type": "integer", "description": "最多列多少个，默认 40", "minimum": 1, "maximum": 200},
            },
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{}, {"path": "app", "limit": 20}],
        keywords=("coverage", "gap", "untested", "覆盖", "缺口", "未测试", "遗漏"),
    )


def _is_test_path(rel: str) -> bool:
    """判断是否测试文件。与 codegraph 的规则保持一致。"""
    p = rel.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return True
    parts = p.split("/")[:-1]
    return any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in parts)


# =============================================================================
# 工具：跑生成的测试
# =============================================================================


def _tool_run_generated_tests(workspace: Workspace, runner: CommandRunner) -> Tool:
    """把模型生成的测试代码写进**临时目录**跑一遍，拿到真实结果。

    为什么必须写盘才能跑：pytest 需要文件。为什么写临时目录而不是项目里：
    不能让「验证测试是否成立」这件事污染被审仓库 —— 那是审查对象，
    不是我们该改的代码。
    """

    def handler(code: str, filename: str = "test_generated.py") -> ToolResult:
        import shutil

        if not (code or "").strip():
            return ToolResult.failure("EmptyCode", "测试代码为空。", {},
                                      suggestion="传入完整的 pytest 测试模块源码。")

        # 文件名做最小清洗：只要文件名，不要路径（防目录穿越）
        safe = Path(filename).name
        if not safe.endswith(".py"):
            safe += ".py"

        # 临时目录必须建在**工作区内部**。
        #
        # 之前的版本用 tempfile.mkdtemp()（系统 /tmp），实测 7 次全部失败：
        # CommandRunner 会校验 cwd 落在工作区内，系统临时目录直接撞 PathEscape，
        # 测试代码一次都没被执行 —— 而模型会如实报告「工具侧故障」，
        # 于是整批用例都停在 unverified。这是实现 bug，不是模型的问题。
        #
        # 放在工作区下的隐藏目录里，并且**跑完立即删除**：
        # 满足「可执行」与「不污染仓库」两个要求。
        tmp = workspace.root / ".margin-test-tmp"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            # 测试要能 import 被测代码。执行器不支持传 PYTHONPATH（它只做白名单 + argv），
            # 所以在测试文件头部注入 sys.path 插入。这比改执行器更稳：
            # 执行器是安全边界，不该为单个工具开「自定义环境变量」的口子。
            root_literal = repr(str(workspace.root))
            injected = (
                "import sys\n"
                f"sys.path.insert(0, {root_literal})\n"
                "\n"
                + code
            )
            (tmp / safe).write_text(injected, encoding="utf-8")
            argv = ["python", "-m", "pytest", safe, "-q", "--no-header",
                    "-p", "no:cacheprovider"]
            result = runner.run(argv, cwd=str(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        text = result.render()
        tail = text.strip()[-2500:]

        # runner 的失败只覆盖「命令没跑起来」（白名单/超时/找不到可执行文件）。
        # 「跑完了但 pytest 返回非 0」在 runner 看来是 success —— 它只负责执行，
        # 不判断业务成败。所以必须**自己解析退出码**，否则会把失败的测试
        # 报成「测试通过」，模型据此以为断言成立，这是最坏的误导。
        m = re.search(r"\(exit=(-?\d+)\)", text)
        exit_code = int(m.group(1)) if m else None

        if not result.ok:
            if result.error_type in ("CommandNotAllowed", "Timeout", "ExecutableNotFound",
                                     "SuspiciousArgument", "PathEscape", "BadWorkingDirectory"):
                return ToolResult.failure(
                    result.error_type or "RunFailed", result.message, result.params,
                    suggestion=result.suggestion or "确认 python 与 pytest 已安装且在允许的命令白名单里。",
                )
            return ToolResult.success("测试命令没有正常执行完成。\n\n" + tail)

        if exit_code == 0:
            return ToolResult.success(
                "测试通过（exit=0）。\n"
                "注意：通过**不等于**这条测试有意义 —— 要看它在错误实现上会不会红。\n\n"
                + tail
            )

        if exit_code == 5:
            # pytest 的 5 = 没收集到任何测试。常见于测试函数名不是 test_ 开头，
            # 或者文件里只有类没有测试函数。
            return ToolResult.success(
                "pytest 收集到 **0 个测试**（exit=5）。\n"
                "通常是测试函数名不以 `test_` 开头，或代码里没有函数级测试。\n"
                "这不是「通过」，也不是「被测代码有错」—— 是测试没写对。\n\n"
                + tail
            )

        return ToolResult.success(
            f"测试**没有全绿**（exit={exit_code}）。\n"
            "失败有两种可能，要先分清：\n"
            "  ① 测试写对了，被测代码确实有这个行为 → 这是有价值的发现；\n"
            "  ② 测试本身写错了（断言写反、import 路径错、夹具缺失）→ 改测试，不是改代码。\n\n"
            + tail
        )

    return Tool(
        name="run_generated_tests",
        summary="把生成的测试代码写进临时目录并真实跑一遍",
        description="""跑一段你刚写的测试代码，拿到真实的通过/失败结果。

何时用：
- 写好测试代码后，想确认它**能跑**（语法、import、夹具都对）。
- 想验证一条测试确实会在当前实现上失败（这是它作为证据的前提）。

何时不用：
- 想跑项目**已有的**测试 → 用 run_tests（跑的是仓库里的真实测试）。
- 还没写测试代码 → 先写。

边界：
- 测试代码写到**临时目录**，跑完即删，**不会污染被审仓库**。
- 只能跑 Python/pytest。
- **通过不等于有意义**：一条在错误实现上也能通过的测试，不构成证据。
  所以本工具在通过时也会提醒你看「它在什么情况下会红」。
- 失败时不要急着改代码：先分清是「测试写错」还是「代码有缺陷」。

Returns: 退出码与 pytest 输出的尾部（最多 2500 字）。""",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "完整的 pytest 测试模块源码"},
                "filename": {"type": "string", "description": "临时文件名，默认 test_generated.py"},
            },
            "required": ["code"],
        },
        handler=handler,
        risk=Risk.MEDIUM,  # 会执行代码
        examples=[{
            "code": "import pytest\nfrom pkg.core import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        }],
        keywords=("run", "generated", "verify", "跑测试", "验证", "执行"),
    )


# =============================================================================
# 组装
# =============================================================================

#: 测试助手的核心工具（进静态前缀）。刻意比审查助手少：
#: 核心工具的每一个都占前缀预算，而前缀是每轮都要重发的。
CORE_TOOLS: tuple[str, ...] = (
    "list_files",
    "read_file",
    "grep",
    "git_diff",
    "read_symbols",
    "record_test_case",
    # 验证工具**必须进核心**：实测发现它躺在按需工具里时，模型不知道它存在，
    # 于是 42 条用例全部只有描述、一条都没跑过。渐进披露在这里是反效果 ——
    # 「验证」是这个助手的核心动作，不是可选能力。
    "run_generated_tests",
    "update_todo",
    "finish",
    "discover_tools",
)


def build_test_toolbox(workspace: Workspace, state: RunState,
                       runner: CommandRunner) -> ToolRegistry:
    """构建测试助手工具箱。

    与审查工具箱的差别：
    - **没有 record_finding**（产出是 test_case，不是 finding）
    - **没有 apply_patch**（测试助手不改生产代码；生成测试走临时目录）
    - 多了 check_coverage（机械扫描）与 run_generated_tests（跑生成的测试）
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
    registry.register(_tool_record_test_case(state), core=True)
    registry.register(_tool_run_generated_tests(workspace, runner), core=True)
    registry.register(_tool_update_todo(state), core=True)
    registry.register(_tool_finish(state, outcome="test_plan"), core=True)

    # 按需加载：这些工具不是每次都要用
    registry.register(_tool_check_coverage(workspace, state))
    registry.register(_tool_read_skill(workspace))

    # 刻意**不注册** run_command：它是 HIGH 风险（白名单命令里 python 能代写文件、
    # git 能 checkout 掉改动）。测试助手的执行需求已经由 run_generated_tests 覆盖
    # —— 那个工具只写临时目录、跑完即删。
    # 加回来之前先想清楚：这个助手凭什么需要执行任意命令？答案通常是「不需要」。

    # 代码图：只读那三个查询（测试助手不需要自己建图，复用审查助手建好的）
    for tool in build_graph_toolbox(workspace, readonly=True).all():
        if registry.get(tool.name) is None:
            registry.register(tool)

    return registry


__all__ = ["build_test_toolbox", "CORE_TOOLS"]
