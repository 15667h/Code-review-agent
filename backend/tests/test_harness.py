"""回归测试。

覆盖重点是那些「改坏了不会立刻报错、但会让结论失去可信度」的地方：
路径沙箱、参数校验、截断语义、规则精度、评估指标、上下文前缀稳定性。

其中两个指标用例直接对照书中的算例，用来确认实现口径正确。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from eval.dataset import ExpectedFinding, match_findings  # noqa: E402
from eval.metrics import (  # noqa: E402
    RunMatrix,
    confidence_interval,
    mcnemar,
    pass_at_k,
    pass_pow_k,
    standard_error,
)
from harness import rules  # noqa: E402
from harness.budget import Budget, estimate_tokens  # noqa: E402
from harness.context import (  # noqa: E402
    COMPRESSED_TAG,
    ContextBuilder,
    compress_trajectory,
    goal_message,
    loaded_tools_message,
    render_status,
    summarize_tool_output,
    validate_protocol,
)
from harness.config import Config  # noqa: E402
from harness.guardrails import Constrainer, Corrector, check_completion_claims  # noqa: E402
from harness.loop import resolve_workspace, run_audit  # noqa: E402
from harness.model import ScriptedModel  # noqa: E402
from harness.report import sort_findings  # noqa: E402
from harness.sandbox import PathEscape, Workspace  # noqa: E402
from harness.tools import Risk, Tool, ToolRegistry, truncate_output, validate_arguments  # noqa: E402
from harness.types import Decision, Finding, Message, RunState, ToolCall, ToolResult  # noqa: E402

SAMPLE_REPO = BACKEND / "examples" / "vulnerable-service" / "repo"


# =============================================================================
# 沙箱
# =============================================================================


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret\n", encoding="utf-8")
    return Workspace(root=tmp_path / "src")


def test_workspace_rejects_parent_traversal(workspace: Workspace) -> None:
    with pytest.raises(PathEscape):
        workspace.resolve("../outside.txt")


def test_workspace_rejects_absolute_escape(workspace: Workspace) -> None:
    with pytest.raises(PathEscape):
        workspace.resolve("C:/Windows/System32/drivers/etc/hosts")


def test_workspace_allows_nested_path(workspace: Workspace) -> None:
    resolved = workspace.resolve("app.py")
    assert resolved.name == "app.py"
    assert workspace.relative(resolved) == "app.py"


def test_iter_files_skips_noise_dirs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "__pycache__").mkdir(parents=True)
    (root / "__pycache__" / "junk.py").write_text("x=1\n", encoding="utf-8")
    (root / "keep.py").write_text("x=1\n", encoding="utf-8")

    workspace = Workspace(root=root)
    files, truncated = workspace.iter_files(".", "*.py")
    names = [workspace.relative(item) for item in files]
    assert names == ["keep.py"]
    assert truncated is False


# =============================================================================
# 参数校验：只报告问题，不做静默修正
# =============================================================================


def test_validate_arguments_reports_missing_required() -> None:
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    assert validate_arguments(schema, {}) == ["缺少必填参数 `path`"]


def test_validate_arguments_reports_type_mismatch() -> None:
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    errors = validate_arguments(schema, {"limit": "ten"})
    assert errors and "应为 integer" in errors[0]


def test_validate_arguments_rejects_boolean_as_integer() -> None:
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    assert validate_arguments(schema, {"limit": True})


def test_registry_rejects_malformed_json_arguments() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(name="noop", description="d", parameters={"type": "object", "properties": {}},
             handler=lambda: ToolResult.success("ok"))
    )
    result = registry.execute(ToolCall(name="noop", arguments={"__unparsed__": "{not json"}))
    assert not result.ok
    assert result.error_type == "MalformedArguments"


def test_registry_error_mentions_available_tools() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(name="read_file", description="d", parameters={"type": "object", "properties": {}},
             handler=lambda: ToolResult.success("ok"))
    )
    result = registry.execute(ToolCall(name="nope", arguments={}))
    assert not result.ok
    assert "read_file" in (result.suggestion or "")


# =============================================================================
# 截断语义
# =============================================================================


def test_truncate_keeps_head_and_tail(tmp_path: Path) -> None:
    text = "\n".join(f"line {i}" for i in range(1, 401))
    rendered, truncated, saved = truncate_output(text, name="big", save_dir=tmp_path)

    assert truncated is True
    assert saved is not None
    assert "line 1" in rendered
    assert "line 400" in rendered
    assert "line 200" not in rendered
    assert "省略" in rendered and "read_file" in rendered
    assert Path(saved).read_text(encoding="utf-8") == text


def test_truncate_passthrough_for_small_output(tmp_path: Path) -> None:
    rendered, truncated, saved = truncate_output("短输出", name="s", save_dir=tmp_path)
    assert rendered == "短输出"
    assert truncated is False and saved is None


# =============================================================================
# 规则：正例与负例
# =============================================================================


def _ids(path: str, source: str) -> list[str]:
    return sorted({hit.rule.id for hit in rules.scan_source(path, source)})


def test_rule_detects_hardcoded_secret() -> None:
    assert "SEC001" in _ids("app/a.py", 'API_KEY = "sk-abcdef123456"\n')


def test_rule_ignores_secret_in_test_file() -> None:
    assert "SEC001" not in _ids("tests/test_a.py", 'PASSWORD = "fixture-password"\n')


def test_rule_detects_sql_concatenation_both_forms() -> None:
    assert "SEC002" in _ids("a.py", 'sql = "SELECT * FROM t WHERE id = " + uid\n')
    assert "SEC002" in _ids("a.py", 'sql = f"SELECT * FROM t WHERE id = {uid}"\n')


def test_rule_ignores_parameterized_query() -> None:
    assert "SEC002" not in _ids("a.py", 'conn.execute("DELETE FROM t WHERE token = %s", (token,))\n')


def test_rule_ignores_sql_keyword_in_plain_string() -> None:
    assert "SEC002" not in _ids("a.py", 'MSG = "SELECT failed"\n')


def test_rule_ignores_keyword_in_comment() -> None:
    """这条是本项目真实踩过的坑：注释里出现关键字不应影响判定。"""
    source = "handle = open(p)  # 应该改成 with open(p)\n"
    assert "RES001" in _ids("a.py", source)


def test_rule_does_not_flag_release_when_finally_present() -> None:
    source = (
        "def f():\n"
        "    conn = pool.acquire()\n"
        "    try:\n"
        "        pass\n"
        "    finally:\n"
        "        pool.release(conn)\n"
    )
    assert "RES002" not in _ids("a.py", source)


def test_rule_flags_acquire_without_finally() -> None:
    source = "def f():\n    conn = pool.acquire()\n    return conn\n"
    assert "RES002" in _ids("a.py", source)


def test_rule_ignores_assert_in_tests() -> None:
    assert "PY002" not in _ids("tests/test_x.py", "def test_a():\n    assert 1 == 1\n")


def test_rule_redacts_secret_in_evidence() -> None:
    hits = rules.scan_source("a.py", 'PASSWORD = "super-secret-value"\n')
    hit = next(item for item in hits if item.rule.id == "SEC001")
    assert "super-secret-value" not in hit.evidence


def test_scan_tree_on_sample_repo_finds_expected_rules() -> None:
    report = rules.scan_tree(SAMPLE_REPO)
    found = {hit.rule.id for hit in report.hits}
    for expected in {"SEC001", "SEC002", "SEC003", "ERR001", "PY001", "PY003"}:
        assert expected in found, f"样本仓库应命中 {expected}"
    assert report.scanned_files >= 5


# =============================================================================
# 靶场纯净性：被审代码里绝不能出现答案
# =============================================================================


def test_audit_roots_do_not_leak_labels() -> None:
    """被审计的代码里不能出现标注或提示词。

    这条守的是一个真实犯过的错：标注原本写在缺陷行的行尾注释里，
    模型读文件时直接看到了缺陷编号与说明 —— 等于把答案印在题目下面。
    那种布局下实测召回率虚高到 100%，量出来的是「会不会照着注释抄」，
    不是审计能力。标注现在放在审计根之外的 labels.json，本测试守住这个边界。
    """
    from eval.dataset import load_default_cases

    cases = load_default_cases(BACKEND / "examples")
    assert len(cases) >= 1, "至少要有一个样本"

    for case in cases:
        assert case.expected, f"{case.id} 没有解析出任何标注"
        assert not (case.repo / "labels.json").exists(), (
            f"{case.id} 的 labels.json 落在审计根之内，模型能读到答案"
        )
        for path in case.repo.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "# 缺陷" not in text, f"{path} 仍含标注注释"
            for keyword in ("靶场", "故意包含", "标注"):
                assert keyword not in text, f"{path} 含提示词「{keyword}」"


def test_sidecar_labels_point_at_real_defect_lines() -> None:
    """labels.json 里的行号必须指向实际存在的行，且不能被整体搬空。"""
    from eval.dataset import load_default_cases

    for case in load_default_cases(BACKEND / "examples"):
        for label in case.expected:
            target = case.repo / label.file
            assert target.is_file(), f"{case.id} 的标注指向不存在的文件 {label.file}"
            body = target.read_text(encoding="utf-8").splitlines()
            assert 1 <= label.line <= len(body), (
                f"{case.id} {label.file}:{label.line} 超出文件行数 {len(body)}"
            )
            assert body[label.line - 1].strip(), (
                f"{case.id} {label.file}:{label.line} 指向空行，标注位置可能已漂移"
            )


# =============================================================================
# 发现与去重
# =============================================================================


def test_finding_dedup_by_fingerprint() -> None:
    state = RunState(goal="g")
    first = Finding(severity="warn", file="a.py", line=3, title="T", detail="d", evidence="e")
    duplicate = Finding(severity="warn", file="a.py", line=3, title="T", detail="d", evidence="e")
    assert state.add_finding(first) is True
    assert state.add_finding(duplicate) is False
    assert len(state.findings) == 1


def test_sort_findings_orders_by_severity_then_location() -> None:
    items = [
        Finding(severity="note", file="b.py", line=9, title="n", detail="", evidence=""),
        Finding(severity="blocker", file="z.py", line=1, title="b", detail="", evidence=""),
        Finding(severity="warn", file="a.py", line=2, title="w", detail="", evidence=""),
    ]
    ordered = sort_findings(items)
    assert [item.severity for item in ordered] == ["blocker", "warn", "note"]


# =============================================================================
# 上下文与压缩
# =============================================================================


def test_prefix_fingerprint_is_stable() -> None:
    builder = ContextBuilder(system_prompt="S", core_tools=[{"type": "function"}], budget=Budget(1000))
    assert builder.prefix_fingerprint == builder.prefix_fingerprint
    other = ContextBuilder(system_prompt="S ", core_tools=[{"type": "function"}], budget=Budget(1000))
    assert builder.prefix_fingerprint != other.prefix_fingerprint


def test_build_places_status_last() -> None:
    builder = ContextBuilder(system_prompt="S", core_tools=[], budget=Budget(1000))
    state = RunState(goal="g")
    messages = builder.build([goal_message("目标")], Message(role="user", content=render_status(state)))
    assert messages[0].role == "system"
    assert messages[-1].content.startswith("<agent_status>")


def test_status_bar_is_maintained_by_code() -> None:
    state = RunState(goal="g", max_iterations=10)
    state.iteration = 3
    state.note_tool("grep:{}")
    state.note_tool("grep:{}")
    region = render_status(state)
    assert "iteration: 3/10" in region
    assert "grep=2" in region
    assert "连续 2 次调用同一个" in region


def test_compress_replaces_long_tool_output_and_marks_it() -> None:
    long_text = "\n".join(f"row {i}" for i in range(600))
    trajectory = [
        Message(role="assistant", content="看这个文件"),
        Message(role="tool", content=long_text, tool_call_id="c1", name="read_file"),
    ] + [Message(role="tool", content=f"small {i}", tool_call_id=f"c{i}") for i in range(1, 12)]

    compressed, stats = compress_trajectory(trajectory, keep_recent=4)
    body = next(m for m in compressed if m.tool_call_id == "c1")
    assert stats["compressed"] == 1
    assert COMPRESSED_TAG in body.content
    assert body.compressed is True
    assert body.raw_content == long_text


def test_compress_keeps_failure_evidence() -> None:
    failure = "Timeout: 命令执行超过 120 秒已被终止。\n" + "x" * 4000
    trajectory = [
        Message(role="tool", content=failure, tool_call_id="c1", name="run_tests"),
    ] + [Message(role="tool", content=f"ok {i}", tool_call_id=f"c{i}") for i in range(1, 12)]

    compressed, _ = compress_trajectory(trajectory, keep_recent=4)
    kept = next(m for m in compressed if m.tool_call_id == "c1")
    assert COMPRESSED_TAG not in kept.content


def test_summarize_keeps_head_tail_and_error_lines() -> None:
    text = "\n".join(["head"] * 20 + ["ERROR: something failed"] + ["body"] * 400 + ["tail"])
    summary = summarize_tool_output(text, max_chars=200)
    assert "head" in summary and "tail" in summary
    assert "ERROR" in summary
    assert COMPRESSED_TAG in summary


def test_budget_trigger_ratio() -> None:
    budget = Budget(window=1000, trigger_ratio=0.8)
    assert budget.trigger_at == 800
    assert budget.should_compress(200, [Message(role="tool", content="x" * 2500)]) is True
    assert budget.should_compress(100, [Message(role="user", content="hi")]) is False


def test_estimate_tokens_prefers_over_estimate_for_cjk() -> None:
    assert estimate_tokens("中文字符" * 25) >= 100
    assert estimate_tokens("abcdefghijklmnopqrst") < 12


# =============================================================================
# 护栏
# =============================================================================


def test_constrainer_blocks_high_risk_by_default() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(name="run_command", description="d", parameters={"type": "object", "properties": {}},
             handler=lambda: ToolResult.success("x"), risk=Risk.HIGH)
    )
    constrainer = Constrainer(registry=registry)
    decision = constrainer.check(ToolCall(name="run_command", arguments={}), RunState(goal="g"))
    assert decision.allowed is False
    assert decision.result is not None and decision.result.error_type == "HighRiskBlocked"


def test_constrainer_breaks_repeated_identical_calls() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(name="grep", description="d", parameters={"type": "object", "properties": {}},
             handler=lambda: ToolResult.success("x"))
    )
    state = RunState(goal="g")
    constrainer = Constrainer(registry=registry, repeat_limit=3)
    call = ToolCall(name="grep", arguments={"pattern": "x"})
    for _ in range(3):
        state.note_tool(call.signature())
    decision = constrainer.check(call, state)
    assert decision.allowed is False
    assert decision.result is not None and decision.result.error_type == "RepeatedCall"


def _registry_with(*specs: tuple[str, Risk]) -> ToolRegistry:
    registry = ToolRegistry()
    for name, risk in specs:
        registry.register(
            Tool(
                name=name,
                description="d",
                parameters={"type": "object", "properties": {}},
                handler=lambda: ToolResult.success("ok"),
                risk=risk,
            )
        )
    return registry


def test_reject_circuit_exempts_finish_and_record_finding() -> None:
    """真实运行踩过的故障：熔断打开后连 finish 都被拒，发现全部丢失。

    熔断的目的是「别让模型把预算烧死在被禁操作上」，不是切断它的全部能力。
    """
    registry = _registry_with(
        ("run_command", Risk.HIGH),
        ("finish", Risk.LOW),
        ("record_finding", Risk.LOW),
        ("update_todo", Risk.LOW),
        ("read_file", Risk.LOW),
    )
    constrainer = Constrainer(registry=registry)
    state = RunState(goal="g")
    state.reject_streak = 99  # 模拟熔断已打开

    assert constrainer.check(ToolCall(name="run_command", arguments={}), state).allowed is False
    assert constrainer.check(ToolCall(name="read_file", arguments={}), state).allowed is False
    # 收尾与落库必须始终可用
    assert constrainer.check(ToolCall(name="finish", arguments={}), state).allowed is True
    assert constrainer.check(ToolCall(name="record_finding", arguments={}), state).allowed is True
    assert constrainer.check(ToolCall(name="update_todo", arguments={}), state).allowed is True


def test_run_audit_keeps_findings_when_reject_circuit_opens(tmp_path: Path) -> None:
    """端到端回归：熔断打开后，模型仍能落库发现并正常收尾。

    真实运行里的表现是：模型连试几次高风险工具 → 熔断 → record_finding 与 finish
    全部被拒 → 14 条查实的发现一条都没进报告，循环只能撞到 max_iterations。
    """
    (tmp_path / "app.py").write_text("SECRET = 'hardcoded'\n", encoding="utf-8")
    workspace = resolve_workspace(tmp_path)

    script = [
        # 第 1 轮：一批 6 个高风险调用（参数各不相同，确保走的是风险拒绝而非重复熔断）
        Decision(
            tool_calls=[
                ToolCall(name="run_command", arguments={"argv": ["pytest", f"-k={i}"]}, id=f"r{i}")
                for i in range(6)
            ]
        ),
        # 第 2 轮：记录一条发现 —— 熔断后仍必须成功
        Decision(
            tool_calls=[
                ToolCall(
                    name="record_finding",
                    arguments={
                        "severity": "blocker",
                        "file": "app.py",
                        "line": 1,
                        "title": "硬编码密钥",
                        "detail": "凭据进入版本历史后只能靠轮换撤回。",
                        "evidence": "SECRET = 'hardcoded'",
                    },
                    id="f1",
                )
            ]
        ),
        # 第 3 轮：收尾 —— 同样必须成功
        Decision(
            tool_calls=[
                ToolCall(
                    name="finish",
                    arguments={"summary": "已检查 app.py，发现 1 条阻断项。"},
                    id="fin1",
                )
            ]
        ),
    ]

    config = Config(api_key="test-key", max_iterations=8)
    result = run_audit(goal="审查仓库", workspace=workspace, model=ScriptedModel(script), config=config)

    assert result.stop_reason == "finished", result.warnings
    assert len(result.findings) == 1
    assert result.findings[0].severity == "blocker"
    # 被拒次数仍要如实记进汇报
    assert any("被护栏拒绝" in item for item in result.warnings)


def test_run_audit_resets_reject_streak_after_allowed_call(tmp_path: Path) -> None:
    """连续被拒计数必须在正常工具放行后归零，否则熔断永不关闭。

    这里刻意排成「被拒 3 次 → 正常读取 → 再被拒 3 次」：
    累计已 6 次、超过阈值 5。若判据用的是累计值，后半段就会变成 CircuitOpen；
    正确的连续语义下，后半段每次都应只报 HighRiskBlocked。
    """
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    workspace = resolve_workspace(tmp_path)

    def rejected_batch(prefix: str, count: int) -> Decision:
        return Decision(
            tool_calls=[
                ToolCall(name="run_command", arguments={"argv": [f"{prefix}{i}"]}, id=f"{prefix}{i}")
                for i in range(count)
            ]
        )

    def read(call_id: str) -> Decision:
        return Decision(tool_calls=[ToolCall(name="read_file", arguments={"path": "app.py"}, id=call_id)])

    script = [
        rejected_batch("a", 3),
        read("r1"),
        rejected_batch("b", 3),
        read("r2"),
        Decision(tool_calls=[ToolCall(name="finish", arguments={"summary": "无发现，已检查 app.py。"}, id="z")]),
    ]

    config = Config(api_key="test-key", max_iterations=10)
    result = run_audit(goal="审查仓库", workspace=workspace, model=ScriptedModel(script), config=config)

    assert result.stop_reason == "finished", result.warnings
    rendered = "\n".join(m.content for m in result.trajectory if m.role == "tool")
    assert "HighRiskBlocked" in rendered
    assert "CircuitOpen" not in rendered, rendered
    assert any("6 次工具调用被护栏拒绝" in item for item in result.warnings), result.warnings


def test_corrector_escalates_then_circuit_breaks() -> None:
    corrector = Corrector(failure_limit=3)
    call = ToolCall(name="read_file", arguments={})
    failure = ToolResult.failure("FileNotFound", "nope", {})

    assert corrector.observe(call, failure) == ""
    assert "连续" in corrector.observe(call, failure)
    assert "停止继续尝试工具" in corrector.observe(call, failure)
    assert corrector.observe(call, ToolResult.success("ok")) == ""
    assert corrector.consecutive_failures == 0


def test_completion_check_catches_unreported_empty_result() -> None:
    state = RunState(goal="g")
    problems = check_completion_claims(state, "审计完成。")
    assert any("发现数为 0" in item for item in problems)


def test_completion_check_passes_when_limitations_stated() -> None:
    state = RunState(goal="g")
    assert check_completion_claims(state, "本次未发现问题，检查范围是 src/ 下全部文件。") == []


# =============================================================================
# 评估指标（对照书中算例）
# =============================================================================


def test_pass_metrics_match_book_example() -> None:
    """书中算例：p=0.6, k=5 → Pass@5 ≈ 99.0%，Pass^5 ≈ 7.8%。"""
    assert pass_at_k(0.6, 5) == pytest.approx(0.990, abs=0.001)
    assert pass_pow_k(0.6, 5) == pytest.approx(0.0778, abs=0.001)


def test_confidence_interval_matches_book_example() -> None:
    """书中算例：n=100, p=70% → 95% 置信区间约为 70% ± 9 个百分点。"""
    low, high = confidence_interval(0.7, 100)
    assert standard_error(0.7, 100) == pytest.approx(0.046, abs=0.001)
    assert low == pytest.approx(0.610, abs=0.005)
    assert high == pytest.approx(0.790, abs=0.005)


def test_mcnemar_detects_asymmetry() -> None:
    a = [True] * 10 + [False] * 10
    b = [False] * 10 + [False] * 10
    result = mcnemar(a, b)
    assert result.a_only == 10 and result.b_only == 0
    assert result.p_value < 0.05


def test_mcnemar_reports_no_difference() -> None:
    a = [True, False, True, False]
    b = [True, False, True, False]
    result = mcnemar(a, b)
    assert result.discordant == 0
    assert "无法区分" in result.describe()


def test_run_matrix_pass_metrics() -> None:
    matrix = RunMatrix()
    matrix.add("case-a", [True, True, True])   # 稳定通过
    matrix.add("case-b", [True, False, True])  # 不稳定
    matrix.add("case-c", [False, False, False])  # 稳定失败

    # Pass@1 是「每题通过率」的均值：(3/3 + 2/3 + 0/3) / 3 = 5/9
    assert matrix.pass_at_1() == pytest.approx(5 / 9, abs=1e-6)
    assert matrix.pass_hat_k(3) == pytest.approx(1 / 3, abs=1e-6)
    assert matrix.pass_at_k_any(3) == pytest.approx(2 / 3, abs=1e-6)


# =============================================================================
# 匹配算法
# =============================================================================


class _Predicted:
    def __init__(self, file: str, line: int) -> None:
        self.file = file
        self.line = line


def test_match_findings_consumes_each_expectation_once() -> None:
    """早期版本会让多条预测反复匹配同一条标注，导致后续标注被误判为漏报。"""
    expected = [
        ExpectedFinding(code="X1-a", file="a.py", line=10, description=""),
        ExpectedFinding(code="X1-b", file="a.py", line=11, description=""),
    ]
    predicted = [_Predicted("a.py", 10), _Predicted("a.py", 11)]
    outcome = match_findings(predicted, expected)
    assert len(outcome.matched) == 2
    assert outcome.missed == []
    assert outcome.spurious == []


def test_match_findings_reports_missed_and_spurious() -> None:
    expected = [ExpectedFinding(code="X1-a", file="a.py", line=10, description="")]
    predicted = [_Predicted("a.py", 40), _Predicted("b.py", 10)]
    outcome = match_findings(predicted, expected, line_tolerance=1)
    assert outcome.missed and len(outcome.missed) == 1
    assert len(outcome.spurious) == 2
    assert outcome.precision == 0.0 and outcome.recall == 0.0


def test_grouping_instance_keeps_merged_report_as_miss() -> None:
    """逐实例口径下，一条预测只能覆盖一条标注。

    这正是真实评估里 guards.py 那 3 条「漏报」的成因：模型把
    L17/L18/L24 三个 assert 合并成一条报告，逐条口径判它漏了两条。
    """
    expected = [
        ExpectedFinding(code="PY002-a", file="g.py", line=17, description=""),
        ExpectedFinding(code="PY002-b", file="g.py", line=18, description=""),
        ExpectedFinding(code="PY002-c", file="g.py", line=24, description=""),
    ]
    predicted = [_Predicted("g.py", 17)]

    strict = match_findings(predicted, expected, grouping="instance")
    assert len(strict.matched) == 1
    assert len(strict.missed) == 2
    assert strict.collateral == []


def test_grouping_file_rule_treats_same_file_same_rule_as_one_unit() -> None:
    """分口径下，同文件同规则号合并为一个缺陷单元，一条预测即算覆盖。"""
    expected = [
        ExpectedFinding(code="PY002-a", file="g.py", line=17, description=""),
        ExpectedFinding(code="PY002-b", file="g.py", line=18, description=""),
        ExpectedFinding(code="PY002-c", file="g.py", line=24, description=""),
    ]
    predicted = [_Predicted("g.py", 17)]

    grouped = match_findings(predicted, expected, grouping="file_rule")
    assert len(grouped.matched) == 1
    assert len(grouped.collateral) == 2      # 另两条实例被同一条预测一并覆盖
    assert grouped.missed == []
    assert grouped.recall == 1.0
    assert grouped.precision == 1.0          # 精确率不受分组影响


def test_grouping_file_rule_keeps_different_files_apart() -> None:
    """同规则号但不同文件，必须仍然是两个缺陷单元。"""
    expected = [
        ExpectedFinding(code="ERR001-a", file="a.py", line=28, description=""),
        ExpectedFinding(code="ERR001-b", file="b.py", line=32, description=""),
    ]
    predicted = [_Predicted("a.py", 28)]
    grouped = match_findings(predicted, expected, grouping="file_rule")
    assert len(grouped.matched) == 1
    assert len(grouped.missed) == 1
    assert grouped.collateral == []
    assert grouped.recall == 0.5


def test_grouping_file_rule_counts_extra_predictions_as_duplicates() -> None:
    """同一单元被多次报告属于冗余，不是误报 —— 不能扣精确率。

    真实场景：规则集把 guards.py 里同一个模式的 3 个 assert 报成 3 条，
    分组口径下这是 1 个缺陷单元 + 2 条冗余；早期实现把它们算成误报，
    把规则集的精确率从 100% 压到 58%。
    """
    expected = [
        ExpectedFinding(code="PY002-a", file="g.py", line=17, description=""),
        ExpectedFinding(code="PY002-b", file="g.py", line=18, description=""),
        ExpectedFinding(code="PY002-c", file="g.py", line=24, description=""),
    ]
    predicted = [_Predicted("g.py", 17), _Predicted("g.py", 18), _Predicted("g.py", 24)]

    grouped = match_findings(predicted, expected, grouping="file_rule")
    assert len(grouped.matched) == 1
    assert len(grouped.collateral) == 2
    assert len(grouped.duplicates) == 2
    assert grouped.spurious == []
    assert grouped.precision == 1.0   # 冗余不计入精确率分母
    assert grouped.recall == 1.0


def test_match_findings_rejects_unknown_grouping() -> None:
    with pytest.raises(ValueError):
        match_findings([], [], grouping="nope")


# =============================================================================
# 消息协议（用真实 API 跑出来的 400 回归）
# =============================================================================


def test_validate_protocol_accepts_well_formed_parallel_calls() -> None:
    messages = [
        Message(role="system", content="s"),
        Message(role="user", content="g"),
        Message(role="assistant", tool_calls=[ToolCall(name="a", id="c1"), ToolCall(name="b", id="c2")]),
        Message(role="tool", tool_call_id="c1", name="a", content="r1"),
        Message(role="tool", tool_call_id="c2", name="b", content="r2"),
        Message(role="user", content="status"),
    ]
    assert validate_protocol(messages) == []


def test_validate_protocol_catches_interleaved_user_message() -> None:
    """这正是真实 API 报 400 的那种形状。

    模型一次调用 discover_tools + grep 时，加载的工具 schema 曾被插在
    两条 tool 结果之间，API 回应：
      "insufficient tool messages following tool_calls message"
    """
    messages = [
        Message(role="assistant", tool_calls=[ToolCall(name="discover_tools", id="c1"), ToolCall(name="grep", id="c2")]),
        Message(role="tool", tool_call_id="c1", name="discover_tools", content="已加载"),
        Message(role="user", content="[tool_discovery] ..."),   # ← 夹在中间
        Message(role="tool", tool_call_id="c2", name="grep", content="31 处匹配"),
    ]
    problems = validate_protocol(messages)
    assert problems, "夹入 user 消息必须被本地校验抓到"
    assert "c2" in problems[0] and "缺少" in problems[0]


def test_validate_protocol_catches_missing_tool_result() -> None:
    messages = [
        Message(role="assistant", tool_calls=[ToolCall(name="a", id="c1"), ToolCall(name="b", id="c2")]),
        Message(role="tool", tool_call_id="c1", name="a", content="r1"),
    ]
    problems = validate_protocol(messages)
    assert problems and "c2" in problems[0]


def test_run_audit_keeps_tool_pairing_when_discovery_is_parallel(tmp_path: Path) -> None:
    """端到端回归：并行工具调用 + 动态加载 schema，整轮必须能正常收尾。

    这条路径在离线模式下永远不会被触发（离线模型逐条调用工具），
    只有真实模型并发调用时才会走到 —— 所以必须有独立的回归用例。
    """
    (tmp_path / "app.py").write_text("SECRET = 'hardcoded'\n", encoding="utf-8")
    workspace = resolve_workspace(tmp_path)

    script = [
        # 第 1 轮：discover_tools 与 grep 在**同一次响应**里并行返回
        Decision(
            tool_calls=[
                ToolCall(name="discover_tools", arguments={"query": "运行测试"}, id="c1"),
                ToolCall(name="grep", arguments={"pattern": "SECRET"}, id="c2"),
            ]
        ),
        # 第 2 轮：收尾
        Decision(
            tool_calls=[
                ToolCall(
                    name="finish",
                    arguments={"summary": "本次未发现阻断问题，检查范围覆盖 app.py 全部内容。"},
                    id="c3",
                )
            ]
        ),
    ]

    config = Config(api_key="test-key", max_iterations=6)
    result = run_audit(goal="审查仓库", workspace=workspace, model=ScriptedModel(script), config=config)

    assert result.stop_reason != "protocol_error", result.warnings
    assert result.stop_reason == "finished"
    # 最终轨迹整体必须满足协议
    full = [Message(role="system", content="sys"), *result.trajectory]
    assert validate_protocol(full) == []

    # 加载的工具 schema 必须出现在**所有同批 tool 结果之后**
    roles = [message.role for message in result.trajectory]
    first_user_after_tools = next(
        index
        for index, message in enumerate(result.trajectory)
        if message.role == "user" and message.content.startswith("[tool_discovery]")
    )
    assert roles[first_user_after_tools - 1] == "tool"
    # 该 user 消息之前，本批两条 tool 结果都应已就位
    assert roles.count("tool") >= 2
    assert result.iterations == 2


# ===========================================================================
# 靶场度量工具
#
# 这一组测试守的是「度量口径」本身。本项目已被度量工具的 bug 坑过两次
# （匹配器重复消费标注、分组口径把冗余算成误报），所以新加的度量逻辑
# 必须有测试，尤其是会把「误报」重新分类成「位置分歧」的那一段 ——
# 判错了就等于偷偷放宽指标。
# ===========================================================================


def test_tolerance_curve_is_monotonic_in_recall() -> None:
    """容差放宽，命中数不能减少 —— 召回率随容差单调不减。"""
    from eval.dataset import ExpectedFinding
    from eval.run_bench import TOLERANCES, tolerance_curve

    expected = [
        ExpectedFinding(code="A1-a", file="a.py", line=10, description=""),
        ExpectedFinding(code="A2-a", file="a.py", line=30, description=""),
    ]
    predicted = [_Predicted("a.py", 12), _Predicted("a.py", 32)]
    curve = tolerance_curve(predicted, expected)

    # 两条预测都各差 2 行 → 容差 0/1 时全漏，容差 ≥2 时全中
    assert curve["0"]["recall"] == 0.0
    assert curve["1"]["recall"] == 0.0
    assert curve["2"]["recall"] == 1.0
    assert curve["4"]["recall"] == 1.0

    recalls = [curve[str(t)]["recall"] for t in TOLERANCES]
    assert recalls == sorted(recalls), f"召回随容差应单调不减，实际 {recalls}"
    # 精确率同样不应随容差下降
    precisions = [curve[str(t)]["precision"] for t in TOLERANCES]
    assert precisions == sorted(precisions), f"精确率随容差应单调不减，实际 {precisions}"


def test_classify_spurious_flags_near_miss_as_position_disagreement() -> None:
    """距离 ≤4 行判为位置分歧，距离远或跨文件判为独立多报。"""
    from eval.dataset import ExpectedFinding
    from eval.run_bench import classify_spurious

    expected = [
        ExpectedFinding(code="A1-a", file="a.py", line=20, description=""),
        ExpectedFinding(code="B1-a", file="b.py", line=5, description=""),
    ]
    # 三种情形：同文件差 3 行、同文件差 40 行、完全不同文件
    spurious = [_Predicted("a.py", 23), _Predicted("a.py", 60), _Predicted("c.py", 7)]
    rows = classify_spurious(spurious, expected)

    assert [row["distance"] for row in rows] == [3, 40, None]
    assert rows[0]["nearest_label"]["code"] == "A1-a"
    assert rows[0]["in_same_file"] is True
    assert rows[1]["distance"] == 40
    # 没有同文件标注时，不能编一个最近标注出来
    assert rows[2]["nearest_label"] is None
    assert rows[2]["in_same_file"] is False


def test_classify_missed_finds_nearest_prediction_in_same_file() -> None:
    """漏报分类要能识别出「模型报了同一处但差几行」这种口径问题。"""
    from eval.dataset import ExpectedFinding
    from eval.run_bench import classify_missed

    missed = [
        ExpectedFinding(code="A1-a", file="a.py", line=50, description=""),
        ExpectedFinding(code="B1-a", file="b.py", line=9, description=""),
    ]
    predicted = [_Predicted("a.py", 47)]
    rows = classify_missed(missed, predicted)

    assert rows[0]["distance"] == 3
    assert rows[0]["nearest_prediction"]["file"] == "a.py"
    # b.py 完全没有预测 → 真漏报，不能瞎指
    assert rows[1]["nearest_prediction"] is None
    assert rows[1]["distance"] is None


def test_bench_samples_cover_eight_increasing_tiers() -> None:
    """靶场清单本身是交付物的一部分，样本缺失会导致文档静默少一节。"""
    from eval.run_bench import SAMPLES

    assert len(SAMPLES) == 8, f"应有 8 个难度样本，实际 {len(SAMPLES)}"
    tiers = [row[1] for row in SAMPLES]
    assert tiers == sorted(tiers), "难度排序必须递增，否则文档里的梯度是假的"
    # 每个样本都必须真实存在，且标注文件在审计根之外
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    for name, _tier, _style, goal in SAMPLES:
        case_dir = backend / "examples" / name
        assert (case_dir / "repo").is_dir(), f"{name} 缺少 repo/ 目录"
        assert (case_dir / "labels.json").is_file(), f"{name} 缺少 labels.json"
        assert not (case_dir / "repo" / "labels.json").exists(), (
            f"{name} 的标注落在了审计根之内 —— 模型能直接读到答案"
        )
        assert goal.strip(), f"{name} 的审计目标为空"
