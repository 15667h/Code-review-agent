"""测试助手（tester）的行为测试。

守四类容易静默出错的东西：

1. **``should_fail_when`` 不能凑数** —— 它是这份产出的可评审性所在。
   写不成「什么条件下会红」，说明这条测试没断言任何东西。
2. **产出类型不能混进 finding** —— 测试用例没有严重级别。混了会让前端
   按审查渲染器显示，读者会以为「这里有 defect」。
3. **离线模式必须有 tester 分支** —— 没有的话会掉进审查路径去调
   ``record_finding``，而测试助手的工具箱里根本没这个工具。
4. **只读契约不能误伤** —— tester 需要执行验证（跑自己写的测试），
   但它不该拿到 ``apply_patch`` / ``run_command``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import agents
from harness.sandbox import CommandRunner, Workspace
from harness.tools_test import _is_test_path, build_test_toolbox
from harness.types import TEST_KINDS, RunState, TestCase


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "app" / "core.py").write_text(
        "def add(a, b):\n    return a + b\n\n\n"
        "def refund(paid, total):\n"
        "    # 故意的：按应付而非实收算\n"
        "    return total\n\n\n"
        "def _internal():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (root / "app" / "util.py").write_text(
        "def helper():\n    return 42\n",
        encoding="utf-8",
    )
    return root


# =============================================================================
# 产出类型
# =============================================================================


def test_test_case_is_a_registered_artifact_type() -> None:
    """``test_case`` 必须注册，否则回放终态时会被当成 dict 丢弃字段。"""
    from harness.types import ARTIFACT_TYPES

    assert ARTIFACT_TYPES.get("test_case") is TestCase


def test_test_case_has_no_severity() -> None:
    """测试用例没有严重级别 —— 这是它与 Finding 的本质区别。"""
    case = TestCase(target="f", file="a.py", line=1, title="t",
                    kind="unit", should_fail_when="参数为 None 时抛错")
    assert not hasattr(case, "severity")


def test_test_case_dedup_fingerprint_uses_target_and_title() -> None:
    """去重按「目标 + 标题」，不按代码文本。

    同一条用例换个写法不该算两条；但锁不同行为的同名用例必须分开。
    """
    a = TestCase(target="f", file="a.py", line=1, title="返回和",
                 kind="unit", should_fail_when="x")
    b = TestCase(target="f", file="a.py", line=9, title="返回和",
                 kind="unit", should_fail_when="y", code="完全不同的代码")
    c = TestCase(target="f", file="a.py", line=1, title="抛异常",
                 kind="unit", should_fail_when="z")
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint


def test_test_kinds_cover_the_four_semantics() -> None:
    assert set(TEST_KINDS) == {"characterization", "unit", "integration", "regression"}


# =============================================================================
# record_test_case 的校验
# =============================================================================


def _tool(repo: Path):
    ws = Workspace(repo)
    state = RunState(goal="g", max_iterations=5)
    # 预置产出桶：真实运行里由 agent 声明的 artifact_kinds 初始化，
    # 这里手工跑工具要自己建，否则 ``artifacts_of`` 读不到东西。
    state.artifacts = {"test_case": []}
    runner = CommandRunner(workspace=ws, allow={"python"}, timeout=30)
    return build_test_toolbox(ws, state, runner), state


def test_record_rejects_short_should_fail_when(repo: Path) -> None:
    """太短 = 没说明条件。拒绝。"""
    tb, _ = _tool(repo)
    r = tb.get("record_test_case").handler(
        target="add", file="app/core.py", line=1, title="两数相加",
        kind="unit", should_fail_when="错时",
    )
    assert r.ok is False
    assert r.error_type == "VagueShouldFailWhen"


def test_record_rejects_should_fail_when_that_repeats_title(repo: Path) -> None:
    """**核心校验**：``should_fail_when`` 不能是 title 的同义复述。

    这条守的是「这个字段有没有信息量」。它被填成复述时，等于没回答
    「什么条件下会红」—— 那这条测试就没有证据强度。
    """
    tb, _ = _tool(repo)
    r = tb.get("record_test_case").handler(
        target="refund", file="app/core.py", line=5,
        title="退款金额按实收计算",
        kind="unit",
        should_fail_when="退款金额按实收计算",
    )
    assert r.ok is False
    assert r.error_type == "VagueShouldFailWhen"


def test_record_accepts_a_concrete_condition(repo: Path) -> None:
    tb, state = _tool(repo)
    r = tb.get("record_test_case").handler(
        target="refund", file="app/core.py", line=5,
        title="退款金额按实收计算",
        kind="unit",
        should_fail_when="paid=0 未付款而 total=100 时仍然退款成功",
    )
    assert r.ok is True
    assert len(state.artifacts_of("test_case")) == 1


def test_characterization_requires_pins_behavior(repo: Path) -> None:
    """特征测试必须说清「锁的是哪个当前实际行为」。"""
    tb, _ = _tool(repo)
    r = tb.get("record_test_case").handler(
        target="refund", file="app/core.py", line=5,
        title="当前实现按应付金额退款",
        kind="characterization",
        should_fail_when="paid=0 而 total=100 时仍然退款 100",
    )
    assert r.ok is False
    assert r.error_type == "MissingPinnedBehavior"

    r2 = tb.get("record_test_case").handler(
        target="refund", file="app/core.py", line=5,
        title="当前实现按应付金额退款",
        kind="characterization",
        should_fail_when="paid=0 而 total=100 时仍然退款 100",
        pins_behavior="return total —— 不看 paid，未付款订单也按全额退",
    )
    assert r2.ok is True


def test_record_rejects_unknown_kind(repo: Path) -> None:
    tb, _ = _tool(repo)
    r = tb.get("record_test_case").handler(
        target="add", file="app/core.py", line=1, title="t",
        kind="whatever", should_fail_when="参数为字符串时抛 TypeError",
    )
    assert r.ok is False
    assert r.error_type == "InvalidKind"
    assert "characterization" in r.suggestion or "unit" in r.suggestion


def test_record_dedups_same_target_and_title(repo: Path) -> None:
    """同一「目标+标题」只留一条。

    注意语义已经变了：早期是「重复就丢弃」，现在是「重复就更新可观测字段」。
    丢弃会让验证状态永远补不上去（见 test_re_registering_updates_verification）。
    """
    tb, state = _tool(repo)
    args = dict(target="add", file="app/core.py", line=1, title="两数相加",
                kind="unit", should_fail_when="任一参数为 None 时抛 TypeError")
    tb.get("record_test_case").handler(**args)
    r = tb.get("record_test_case").handler(**args)
    assert "已存在" in (r.data or "")
    assert len(state.artifacts_of("test_case")) == 1


# =============================================================================
# check_coverage
# =============================================================================


def test_coverage_lists_unreferenced_public_symbols(repo: Path) -> None:
    """没有任何测试文件时，全部公开符号都算未覆盖。"""
    tb, _ = _tool(repo)
    text = tb.get("check_coverage").handler().render()
    assert "add" in text and "refund" in text and "helper" in text
    # 下划线开头的内部实现不算
    assert "_internal" not in text


def test_coverage_marks_symbols_used_by_tests(repo: Path) -> None:
    (repo / "tests" / "test_core.py").write_text(
        "from app.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    tb, _ = _tool(repo)
    text = tb.get("check_coverage").handler().render()
    # add 已被测试引用 → 不该出现在未覆盖清单里
    assert "`add`" not in text
    # refund / helper 仍未覆盖
    assert "`refund`" in text


def test_coverage_says_so_when_no_test_files(repo: Path) -> None:
    """没有测试文件时要明说，而不是只列一堆未覆盖符号让人误解。"""
    tb, _ = _tool(repo)
    text = tb.get("check_coverage").handler().render()
    assert "一个测试文件都没有" in text


def test_is_test_path_matches_common_layouts() -> None:
    assert _is_test_path("tests/test_a.py")
    assert _is_test_path("test_b.py")
    assert _is_test_path("pkg/c_test.py")
    assert _is_test_path("pkg/tests/helper.py")
    assert not _is_test_path("app/core.py")


# =============================================================================
# 工具箱边界
# =============================================================================


def test_toolbox_has_no_patch_or_shell(repo: Path) -> None:
    """tester 不该拿到 apply_patch / run_command。

    它的 ``can_edit=True`` 是为了能执行验证（跑自己写的测试），
    不是为了改代码。写入能力必须被限制在「一个会被删掉的临时目录」里。
    """
    tb, _ = _tool(repo)
    names = set(tb.names())
    assert "apply_patch" not in names
    assert "run_command" not in names
    # 但验证工具必须在
    assert "run_generated_tests" in names


def test_toolbox_has_no_record_finding(repo: Path) -> None:
    """产出是 test_case，不是 finding —— 不该有 record_finding。"""
    tb, _ = _tool(repo)
    assert "record_finding" not in tb.names()
    assert "record_test_case" in tb.names()


def test_finish_knows_the_outcome_is_test_plan(repo: Path) -> None:
    """finish 的统计要按 outcome 走，不能对测试运行说「共 0 条发现」。"""
    tb, state = _tool(repo)
    text = tb.get("finish").handler(summary="done").render()
    assert "0 条发现" not in text


# =============================================================================
# AgentSpec 声明
# =============================================================================


def test_tester_is_registered_with_its_own_outcome() -> None:
    spec = agents.get("tester")
    assert spec.outcome == "test_plan"
    assert spec.artifact_kinds == ("test_case",)
    assert "测试" in spec.name


def test_tester_declares_can_edit_for_verification() -> None:
    """``can_edit=True`` 是刻意的：不执行就跑不了测试，功能会废掉。

    这条测试把「为什么不是只读」钉住，防止后人为了「安全」把它改成 False
    然后发现 run_generated_tests 被 `_enforce_read_only` 拦掉。
    """
    spec = agents.get("tester")
    assert spec.can_edit is True
    assert spec.extra.get("writes") == "temp-only"


def test_all_agents_coexist() -> None:
    """四个 agent 都在册。

    断言写成「至少包含」而不是精确相等：每加一个 agent 就改一次断言
    是纯维护噪音，而这里真正要守的是「不许有人不小心把某个 agent 丢了」。
    """
    assert {"review", "architect", "tester"} <= set(agents.ids())


# =============================================================================
# 离线路径
# =============================================================================


def test_offline_has_a_tester_branch() -> None:
    """离线模式必须显式分派 tester。

    没有分支时会掉进 ``_complete_audit`` 去调 ``record_finding``，
    而测试助手工具箱里没有那个工具 —— 直接报错，且错误信息指向不明。
    """
    from harness.offline import TEST_AGENT, HeuristicModel

    model = HeuristicModel(agent=TEST_AGENT)
    assert hasattr(model, "_complete_test")
    assert TEST_AGENT == "tester"


# =============================================================================
# 验证状态
# =============================================================================


def test_case_defaults_to_unverified(repo: Path) -> None:
    """默认未验证 —— 不能因为「写了代码」就当验证过。

    「42 条用例，0 条跑过」和「42 条都跑通」是两件事。
    默认值必须是 unverified，否则报告会把草稿说成已验证。
    """
    tb, state = _tool(repo)
    tb.get("record_test_case").handler(
        target="add", file="app/core.py", line=1, title="两数相加",
        kind="unit", should_fail_when="任一参数为 None 时抛 TypeError",
        code="def test_a():\n    assert True\n",
    )
    case = state.artifacts_of("test_case")[0]
    assert case.verified == "unverified"


def test_case_can_record_verified_and_output(repo: Path) -> None:
    tb, state = _tool(repo)
    tb.get("record_test_case").handler(
        target="add", file="app/core.py", line=1, title="两数相加",
        kind="unit", should_fail_when="任一参数为 None 时抛 TypeError",
        code="def test_a():\n    assert True\n",
        verified="verified", verify_output="1 passed in 0.02s",
    )
    case = state.artifacts_of("test_case")[0]
    assert case.verified == "verified"
    assert "1 passed" in case.verify_output


def test_case_rejects_bogus_verified_value(repo: Path) -> None:
    tb, state = _tool(repo)
    tb.get("record_test_case").handler(
        target="add", file="app/core.py", line=1, title="两数相加",
        kind="unit", should_fail_when="任一参数为 None 时抛 TypeError",
        verified="maybe",
    )
    assert state.artifacts_of("test_case")[0].verified == "unverified"


def test_case_roundtrips_verification_fields() -> None:
    """序列化往返必须保留验证状态，否则回放报告会变成「全部未验证」。"""
    from harness.types import revive_artifact

    case = TestCase(target="f", file="a.py", line=1, title="t", kind="unit",
                    should_fail_when="x 为负数时抛错",
                    verified="verified", verify_output="1 passed")
    revived = revive_artifact("test_case", case.to_dict())
    assert revived.verified == "verified"
    assert revived.verify_output == "1 passed"


def test_report_surfaces_verification_status() -> None:
    """报告必须显式写出「多少条跑过」，不能只给总数。"""
    from types import SimpleNamespace

    from harness.report import render_test_plan_markdown

    cases = [
        TestCase(target="a", file="a.py", line=1, title="t1", kind="unit",
                 should_fail_when="x 为负数时抛错", verified="verified"),
        TestCase(target="b", file="b.py", line=2, title="t2", kind="unit",
                 should_fail_when="y 溢出时抛错", verified="unverified"),
    ]
    result = SimpleNamespace(
        goal="g", artifacts={"test_case": cases}, iterations=3,
        stop_reason="final_answer", warnings=[], outcome="test_plan",
        summary="", agent="tester", severity_counts={"blocker": 0, "warn": 0, "note": 0},
        findings=[], usage=None, elapsed=1.0, cache_hit_ratio=0,
    )
    md = render_test_plan_markdown(result)
    assert "验证状态" in md
    assert "跑通 1" in md
    assert "未验证 1" in md


def test_report_warns_when_nothing_was_verified() -> None:
    """全部未验证时必须给显式警告。

    否则一份 42 条的清单读起来像是「42 条测试都做好了」，
    而真相是「42 条规格说明，一条都没跑过」。
    """
    from types import SimpleNamespace

    from harness.report import render_test_plan_markdown

    cases = [TestCase(target="a", file="a.py", line=1, title="t", kind="unit",
                      should_fail_when="x 为负数时抛错")]
    result = SimpleNamespace(
        goal="g", artifacts={"test_case": cases}, iterations=1,
        stop_reason="final_answer", warnings=[], outcome="test_plan",
        summary="", agent="tester", severity_counts={"blocker": 0, "warn": 0, "note": 0},
        findings=[], usage=None, elapsed=1.0, cache_hit_ratio=0,
    )
    md = render_test_plan_markdown(result)
    assert "一条都没有被真实执行过" in md


def test_verification_tool_is_core(repo: Path) -> None:
    """``run_generated_tests`` 必须在核心工具里。

    实测教训：它躺在按需工具里时，模型不知道它存在，
    42 条用例全部只有描述、一条都没跑过。验证是核心动作，不是可选能力。
    """
    tb, _ = _tool(repo)
    assert tb.is_core("run_generated_tests")


# =============================================================================
# run_generated_tests 的真实执行
# =============================================================================


@pytest.fixture()
def runnable_repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    return root


def _run_tool(repo: Path):
    ws = Workspace(repo)
    state = RunState(goal="g", max_iterations=3)
    runner = CommandRunner(workspace=ws, allow={"python"}, timeout=60)
    return build_test_toolbox(ws, state, runner).get("run_generated_tests")


def test_generated_test_can_import_target(runnable_repo: Path) -> None:
    """生成的测试要能 import 被测代码（sys.path 注入生效）。"""
    tool = _run_tool(runnable_repo)
    r = tool.handler(code="from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    assert "exit=0" in r.render()
    assert "1 passed" in r.render()


def test_failing_test_is_reported_as_failing(runnable_repo: Path) -> None:
    """**关键**：断言不成立时必须报失败，不能报通过。

    实测踩过的 bug：pytest 返回 exit=1，但 ``CommandRunner`` 认为「命令跑完了」
    就是 success，于是工具如实转述成「测试通过」—— 模型据此以为断言成立。
    这是最坏的误导：一条假绿的测试比没有测试更糟。
    """
    tool = _run_tool(runnable_repo)
    r = tool.handler(
        code="from pkg.calc import add\n\n\ndef test_bad():\n    assert add(1, 2) == 99\n"
    )
    text = r.render()
    assert "没有全绿" in text
    assert "exit=1" in text


def test_zero_collected_tests_is_called_out(runnable_repo: Path) -> None:
    """pytest exit=5（没收集到测试）既不是通过，也不是被测代码有错。"""
    tool = _run_tool(runnable_repo)
    r = tool.handler(code="def not_a_test():\n    pass\n")
    text = r.render()
    assert "0 个测试" in text
    assert "不是「通过」" in text


def test_temp_dir_is_cleaned_up(runnable_repo: Path) -> None:
    """跑完即删，不在被审仓库里留垃圾。"""
    tool = _run_tool(runnable_repo)
    tool.handler(code="def test_a():\n    assert True\n")
    leftovers = [p for p in runnable_repo.iterdir() if "margin-test" in p.name]
    assert leftovers == []


def test_temp_dir_is_inside_workspace(runnable_repo: Path) -> None:
    """临时目录必须在工作区内。

    实测事故：用系统临时目录（``tempfile.mkdtemp``）时，
    ``CommandRunner`` 的 cwd 校验直接撞 ``PathEscape``，
    测试代码一次都没执行 —— 模型连续 7 次尝试全部失败并如实报告
    「工具侧故障」。放在工作区下的隐藏目录才既满足沙箱边界又跑得起来。
    """
    tool = _run_tool(runnable_repo)
    # 跑一次确认可用（间接证明目录位置合法）
    r = tool.handler(code="def test_a():\n    assert True\n")
    assert "exit=0" in r.render()


# =============================================================================
# run_generated_tests 的真实执行
# =============================================================================


@pytest.fixture()
def runnable_repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    return root


def _run_tool(repo: Path):
    ws = Workspace(repo)
    state = RunState(goal="g", max_iterations=3)
    runner = CommandRunner(workspace=ws, allow={"python"}, timeout=60)
    return build_test_toolbox(ws, state, runner).get("run_generated_tests")


def test_generated_test_can_import_target(runnable_repo: Path) -> None:
    """生成的测试要能 import 被测代码（sys.path 注入生效）。"""
    tool = _run_tool(runnable_repo)
    r = tool.handler(code="from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    assert "exit=0" in r.render()
    assert "1 passed" in r.render()


def test_failing_test_is_reported_as_failing(runnable_repo: Path) -> None:
    """**关键**：断言不成立时必须报失败，不能报通过。

    实测踩过的 bug：pytest 返回 exit=1，但 ``CommandRunner`` 认为「命令跑完了」
    就是 success，于是工具如实转述成「测试通过」—— 模型据此以为断言成立。
    这是最坏的误导：一条假绿的测试比没有测试更糟。
    """
    tool = _run_tool(runnable_repo)
    r = tool.handler(
        code="from pkg.calc import add\n\n\ndef test_bad():\n    assert add(1, 2) == 99\n"
    )
    text = r.render()
    assert "没有全绿" in text
    assert "exit=1" in text


def test_zero_collected_tests_is_called_out(runnable_repo: Path) -> None:
    """pytest exit=5（没收集到测试）既不是通过，也不是被测代码有错。"""
    tool = _run_tool(runnable_repo)
    r = tool.handler(code="def not_a_test():\n    pass\n")
    text = r.render()
    assert "0 个测试" in text
    assert "不是「通过」" in text


def test_temp_dir_is_cleaned_up(runnable_repo: Path) -> None:
    """跑完即删，不在被审仓库里留垃圾。"""
    tool = _run_tool(runnable_repo)
    tool.handler(code="def test_a():\n    assert True\n")
    leftovers = [p for p in runnable_repo.iterdir() if "margin-test" in p.name]
    assert leftovers == []


def test_temp_dir_is_inside_workspace(runnable_repo: Path) -> None:
    """临时目录必须在工作区内。

    实测事故：用系统临时目录（``tempfile.mkdtemp``）时，
    ``CommandRunner`` 的 cwd 校验直接撞 ``PathEscape``，
    测试代码一次都没执行 —— 模型连续 7 次尝试全部失败并如实报告
    「工具侧故障」。放在工作区下的隐藏目录才既满足沙箱边界又跑得起来。
    """
    tool = _run_tool(runnable_repo)
    # 跑一次确认可用（间接证明目录位置合法）
    r = tool.handler(code="def test_a():\n    assert True\n")
    assert "exit=0" in r.render()


# =============================================================================
# 重复登记要更新验证状态（而不是丢弃）
# =============================================================================


def test_re_registering_updates_verification(repo: Path) -> None:
    """先登记（未验证）→ 跑通后重新登记 → 验证状态要更新上去。

    **实测踩过的 bug**：早期实现用 ``add_artifact``，重复项直接被丢弃。
    后果是模型先登记一条 unverified，跑通后想补 verified 重新登记 ——
    被当成重复丢掉，验证状态永远补不上去，报告永远显示「未验证」。
    12 条用例全停在 unverified，有一半是这个原因。
    """
    tb, state = _tool(repo)
    args = dict(target="add", file="app/core.py", line=1, title="两数相加",
                kind="unit", should_fail_when="任一参数为 None 时抛 TypeError",
                code="def test_a():\n    assert True\n")
    tb.get("record_test_case").handler(**args)
    assert state.artifacts_of("test_case")[0].verified == "unverified"

    r = tb.get("record_test_case").handler(**args, verified="verified",
                                           verify_output="1 passed in 0.01s")
    cases = state.artifacts_of("test_case")
    assert len(cases) == 1, "重复登记不该新增条目"
    assert cases[0].verified == "verified"
    assert "1 passed" in cases[0].verify_output
    assert "已更新" in (r.data or "")


def test_unverified_cannot_downgrade_verified(repo: Path) -> None:
    """已经验证过的用例，不能用一次未验证的登记把它降级回去。

    验证状态只允许单向前进（unverified → verified/failed），
    因为「跑过」是一个发生过的事实，后来的「没跑」不能抹掉它。
    """
    tb, state = _tool(repo)
    args = dict(target="add", file="app/core.py", line=1, title="两数相加",
                kind="unit", should_fail_when="任一参数为 None 时抛 TypeError")
    tb.get("record_test_case").handler(**args, verified="verified")
    tb.get("record_test_case").handler(**args)  # 又登记一次，没带状态
    assert state.artifacts_of("test_case")[0].verified == "verified"


def test_code_without_verification_gets_a_hint(repo: Path) -> None:
    """有代码却没声明验证 → 给出提示，但不自动改成 verified。

    自动改等于「替模型判断它跑过了」，而我们没有执行记录能证明。
    宁可提示，不可假装确定。
    """
    tb, state = _tool(repo)
    r = tb.get("record_test_case").handler(
        target="add", file="app/core.py", line=1, title="两数相加",
        kind="unit", should_fail_when="任一参数为 None 时抛 TypeError",
        code="def test_a():\n    assert True\n",
    )
    assert "unverified" in (r.data or "")
    assert state.artifacts_of("test_case")[0].verified == "unverified"


# =============================================================================
# schema 与 handler 参数必须一致（本轮踩过的坑）
# =============================================================================


def test_record_test_case_schema_matches_handler(repo: Path) -> None:
    """**schema 必须覆盖 handler 的全部参数**。

    实测事故：我给 handler 加了 ``verified`` / ``verify_output``，也改了函数签名，
    但**忘了改 schema 的 properties**。结果是：模型从 schema 里看不到这两个字段，
    永远不会传，于是 18 条用例全部停在 ``unverified`` ——
    而模型在 summary 里明确写了「18 条全部用 run_generated_tests 真跑通过」。
    结构化字段与人读的总结对不上，报告显示「未验证」。

    这类不一致不会有任何报错，只会让数据静默地缺一半。用测试钉住。
    """
    import inspect

    tb, _ = _tool(repo)
    tool = tb.get("record_test_case")
    declared = set(tool.parameters.get("properties", {}))
    actual = set(inspect.signature(tool.handler).parameters)
    missing = actual - declared
    assert not missing, f"handler 有但 schema 没声明的参数：{sorted(missing)}"


def test_verified_is_accepted_through_schema(repo: Path) -> None:
    """``verified`` 必须出现在 schema 的 enum 里，模型才知道可以填。"""
    tb, _ = _tool(repo)
    props = tb.get("record_test_case").parameters["properties"]
    assert "verified" in props
    assert set(props["verified"].get("enum", [])) == {"verified", "failed", "unverified"}
    assert "verify_output" in props


# =============================================================================
# schema 与 handler 参数必须一致（本轮踩过的坑）
# =============================================================================


def test_record_test_case_schema_matches_handler(repo: Path) -> None:
    """**schema 必须覆盖 handler 的全部参数**。

    实测事故：我给 handler 加了 ``verified`` / ``verify_output``，也改了函数签名，
    但**忘了改 schema 的 properties**。结果是：模型从 schema 里看不到这两个字段，
    永远不会传，于是 18 条用例全部停在 ``unverified`` ——
    而模型在 summary 里明确写了「18 条全部用 run_generated_tests 真跑通过」。
    结构化字段与人读的总结对不上，报告显示「未验证」。

    这类不一致不会有任何报错，只会让数据静默地缺一半。用测试钉住。
    """
    import inspect

    tb, _ = _tool(repo)
    tool = tb.get("record_test_case")
    declared = set(tool.parameters.get("properties", {}))
    actual = set(inspect.signature(tool.handler).parameters)
    missing = actual - declared
    assert not missing, f"handler 有但 schema 没声明的参数：{sorted(missing)}"


def test_verified_is_accepted_through_schema(repo: Path) -> None:
    """``verified`` 必须出现在 schema 的 enum 里，模型才知道可以填。"""
    tb, _ = _tool(repo)
    props = tb.get("record_test_case").parameters["properties"]
    assert "verified" in props
    assert set(props["verified"].get("enum", [])) == {"verified", "failed", "unverified"}
    assert "verify_output" in props
