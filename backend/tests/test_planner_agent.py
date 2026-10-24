"""策划助手（planner）的行为测试。

守三类问题：

1. **``done_when`` 必须可判定** —— 这是这份产出的可验收性所在。
   「优化错误处理」这类写法必须被拒。
2. **``touches`` 必须指到具体位置** —— 只给目录等于「改整个模块」，等于没想清楚。
3. **不能和 architect 混为一谈** —— 计划是「怎么落地」，不是「应该长什么样」。
   工具箱里不该有 apply_patch（计划不是已实施的结果）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import agents
from harness.sandbox import CommandRunner, Workspace
from harness.tools_plan import _looks_unfalsifiable, build_plan_toolbox
from harness.types import PLAN_ROLES, PlanStep, RunState


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "service.py").write_text(
        "def refund(paid, total):\n    return total\n", encoding="utf-8"
    )
    return root


def _tool(repo: Path):
    ws = Workspace(repo)
    state = RunState(goal="g", max_iterations=5)
    state.artifacts = {"plan_step": [], "plan_risk": []}
    runner = CommandRunner(workspace=ws, allow={"python"}, timeout=30)
    return build_plan_toolbox(ws, state, runner), state


# =============================================================================
# 产出类型
# =============================================================================


def test_plan_step_is_registered() -> None:
    from harness.types import ARTIFACT_TYPES

    assert ARTIFACT_TYPES.get("plan_step") is PlanStep


def test_plan_roles_cover_three_views() -> None:
    assert set(PLAN_ROLES) == {"spec", "steps", "risk"}


def test_plan_step_fingerprint_uses_order_and_title() -> None:
    a = PlanStep(order=1, title="改退款", done_when="测试全绿")
    b = PlanStep(order=1, title="改退款", done_when="别的条件")
    c = PlanStep(order=2, title="改退款", done_when="测试全绿")
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint


# =============================================================================
# done_when 的可判定性
# =============================================================================


@pytest.mark.parametrize("text", [
    "优化错误处理",
    "重构支付模块",
    "完善一下文档",
    "改进代码质量",
    "梳理模块边界",
])
def test_vague_done_when_is_detected(text: str) -> None:
    """只有动作词、没有任何可观测信号 → 判为空话。"""
    assert _looks_unfalsifiable(text) is True


@pytest.mark.parametrize("text", [
    "现有 6 条 service 测试全绿",
    "refund 不再读取 order.total",
    "接口返回 400 而不是 500",
    "ruff check 无告警",
    "所有外部调用都包在 try/except 里，新增 3 条异常路径测试通过",
])
def test_concrete_done_when_is_accepted(text: str) -> None:
    """含可观测信号（数字/标识符/命令/测试结果）→ 合法。"""
    assert _looks_unfalsifiable(text) is False


def test_record_rejects_missing_done_when(repo: Path) -> None:
    tb, _ = _tool(repo)
    r = tb.get("record_plan_step").handler(
        order=1, title="改退款", done_when="", touches=["app/service.py"]
    )
    assert r.ok is False
    assert r.error_type == "MissingDoneWhen"


def test_record_rejects_vague_done_when(repo: Path) -> None:
    """**核心校验**：无法判定的完成条件会让整份计划失去可验收性。"""
    tb, _ = _tool(repo)
    r = tb.get("record_plan_step").handler(
        order=1, title="改退款", done_when="优化退款逻辑", touches=["app/service.py"]
    )
    assert r.ok is False
    assert r.error_type == "UnfalsifiableDoneWhen"
    # 建议里要给可判定的写法示范，否则模型只会反复改措辞
    assert "测试" in r.suggestion or "全绿" in r.suggestion


def test_record_accepts_concrete_step(repo: Path) -> None:
    tb, state = _tool(repo)
    r = tb.get("record_plan_step").handler(
        order=1, title="把 refund 的金额来源改为 paid",
        done_when="refund(paid=0, total=100) 返回 0，且现有 6 条测试全绿",
        touches=["app/service.py", "refund"],
        verify="python -m pytest -q",
        evidence="app/service.py:2",
    )
    assert r.ok is True
    steps = state.artifacts_of("plan_step")
    assert len(steps) == 1
    assert steps[0].order == 1


# =============================================================================
# touches 的指向性
# =============================================================================


def test_record_rejects_missing_touches(repo: Path) -> None:
    tb, _ = _tool(repo)
    r = tb.get("record_plan_step").handler(
        order=1, title="改退款", done_when="6 条测试全绿", touches=[]
    )
    assert r.ok is False
    assert r.error_type == "MissingTouches"


def test_record_rejects_directory_only_touches(repo: Path) -> None:
    """只给目录 = 「改整个模块」，说明还没想清楚改哪里。"""
    tb, _ = _tool(repo)
    r = tb.get("record_plan_step").handler(
        order=1, title="改退款", done_when="6 条测试全绿", touches=["app/"]
    )
    assert r.ok is False
    assert r.error_type == "VagueTouches"


def test_record_accepts_file_or_symbol_touches(repo: Path) -> None:
    tb, state = _tool(repo)
    for touches in (["app/service.py"], ["OrderService.refund"], ["app/service.py", "refund"]):
        state.artifacts.clear()
        state.artifacts = {"plan_step": [], "plan_risk": []}
        r = tb.get("record_plan_step").handler(
            order=1, title="改退款", done_when="6 条测试全绿", touches=touches
        )
        assert r.ok is True, f"{touches} 应该被接受：{r.message}"


# =============================================================================
# 顺序连续性
# =============================================================================


def test_order_gap_is_rejected(repo: Path) -> None:
    """跳号会让读者以为漏了步骤。"""
    tb, _ = _tool(repo)
    tb.get("record_plan_step").handler(
        order=1, title="第一步", done_when="6 条测试全绿", touches=["app/service.py"]
    )
    r = tb.get("record_plan_step").handler(
        order=3, title="第三步", done_when="6 条测试全绿", touches=["app/service.py"]
    )
    assert r.ok is False
    assert r.error_type == "OrderGap"
    assert "order=2" in r.suggestion


def test_sequential_order_is_accepted(repo: Path) -> None:
    tb, state = _tool(repo)
    for i in (1, 2, 3):
        r = tb.get("record_plan_step").handler(
            order=i, title=f"第{i}步", done_when="6 条测试全绿", touches=["app/service.py"]
        )
        assert r.ok is True
    assert len(state.artifacts_of("plan_step")) == 3


# =============================================================================
# 前提/风险
# =============================================================================


def test_risk_requires_consequence(repo: Path) -> None:
    """只说「有这个风险」没有信息量，必须说清不成立会怎样。"""
    tb, _ = _tool(repo)
    r = tb.get("record_plan_risk").handler(assumption="只有内部调用方", if_wrong="")
    assert r.ok is False
    assert r.error_type == "MissingIfWrong"


def test_risk_records_consequence_and_blocking(repo: Path) -> None:
    tb, state = _tool(repo)
    tb.get("record_plan_risk").handler(
        assumption="refund 只有内部调用方",
        if_wrong="若有外部调用点，改签名会静默坏掉，需要先加兼容层",
        blocking=True,
    )
    risks = state.artifacts_of("plan_risk")
    assert len(risks) == 1
    assert risks[0].blocking is True
    assert "兼容层" in risks[0].why_it_matters


# =============================================================================
# 计划自检
# =============================================================================


def test_check_gaps_reports_incomplete_plan(repo: Path) -> None:
    tb, _ = _tool(repo)
    tb.get("record_plan_step").handler(
        order=1, title="改退款", done_when="6 条测试全绿", touches=["app/service.py"]
        # 故意不给 verify / why / evidence
    )
    text = tb.get("check_plan_gaps").handler().render()
    assert "缺 verify" in text or "没给 verify" in text
    assert "没写 why" in text or "没给 evidence" in text


def test_check_gaps_flags_zero_risk_plan(repo: Path) -> None:
    """零前提的计划通常说明还没评估过不确定性 —— 要提醒。"""
    tb, _ = _tool(repo)
    tb.get("record_plan_step").handler(
        order=1, title="改退款", done_when="6 条测试全绿", touches=["app/service.py"],
        verify="pytest -q", why="因为未付款也能退", evidence="app/service.py:2",
    )
    text = tb.get("check_plan_gaps").handler().render()
    assert "前提" in text


def test_check_gaps_says_it_does_not_judge_correctness(repo: Path) -> None:
    """自检通过 ≠ 计划可行。这句限定必须在输出里。"""
    tb, _ = _tool(repo)
    tb.get("record_plan_step").handler(
        order=1, title="改退款", done_when="6 条测试全绿", touches=["app/service.py"],
        verify="pytest -q", why="因为未付款也能退", evidence="app/service.py:2",
    )
    tb.get("record_plan_risk").handler(assumption="只有内部调用", if_wrong="要加兼容层")
    text = tb.get("check_plan_gaps").handler().render()
    assert "不代表" in text


# =============================================================================
# Agent 边界
# =============================================================================


def test_planner_is_registered_with_its_own_outcome() -> None:
    spec = agents.get("planner")
    assert spec.outcome == "roadmap"
    assert spec.artifact_kinds == ("plan_step", "plan_risk")


def test_planner_cannot_edit_code(repo: Path) -> None:
    """计划是「建议怎么改」，不是「已经改好了」。"""
    spec = agents.get("planner")
    assert spec.can_edit is False


def test_planner_toolbox_has_no_patch(repo: Path) -> None:
    tb, _ = _tool(repo)
    names = set(tb.names())
    assert "apply_patch" not in names
    # 也不该有其它 agent 的产出登记工具
    assert "record_finding" not in names
    assert "record_test_case" not in names
    assert "record_plan_step" in names


def test_planner_has_impact_tool(repo: Path) -> None:
    """impact 是策划助手最常用的工具：估改动波及范围。"""
    tb, _ = _tool(repo)
    assert "impact" in tb.names()


# =============================================================================
# 渲染
# =============================================================================


def _plan_result(steps, risks, *, stop="final_answer"):
    from types import SimpleNamespace
    return SimpleNamespace(
        goal="改退款", artifacts={"plan_step": steps, "plan_risk": risks},
        iterations=5, stop_reason=stop, warnings=[], outcome="roadmap",
        summary="", agent="planner",
        severity_counts={"blocker": 0, "warn": 0, "note": 0},
        findings=[], usage=None, elapsed=1.0, cache_hit_ratio=0,
    )


def test_markdown_shows_done_when_and_verify() -> None:
    from harness.report import render_roadmap_markdown

    step = PlanStep(order=1, title="改退款金额来源", done_when="6 条测试全绿",
                    touches=["app/service.py"], verify="pytest -q")
    md = render_roadmap_markdown(_plan_result([step], []))
    assert "做完是什么样" in md
    assert "6 条测试全绿" in md
    assert "怎么验证" in md


def test_markdown_warns_when_no_verify() -> None:
    """没给验证手段时要显式说明「做完后无法自动确认」。"""
    from harness.report import render_roadmap_markdown

    step = PlanStep(order=1, title="改退款", done_when="6 条测试全绿",
                    touches=["app/service.py"])
    md = render_roadmap_markdown(_plan_result([step], []))
    assert "未给出" in md


def test_markdown_puts_risks_before_steps() -> None:
    """前提要排在步骤前面：读者先要知道计划建立在什么假设上。"""
    from harness.report import render_roadmap_markdown

    step = PlanStep(order=1, title="改退款", done_when="6 条测试全绿",
                    touches=["app/service.py"], verify="pytest -q")
    risks = [{"question": "只有内部调用方", "why_it_matters": "要加兼容层", "blocking": True}]
    md = render_roadmap_markdown(_plan_result([step], risks))
    assert md.index("前提与风险") < md.index("## 步骤")
    assert "阻塞" in md


def test_markdown_flags_zero_risk_plan() -> None:
    from harness.report import render_roadmap_markdown

    step = PlanStep(order=1, title="改退款", done_when="6 条测试全绿",
                    touches=["app/service.py"], verify="pytest -q")
    md = render_roadmap_markdown(_plan_result([step], []))
    assert "没有登记任何前提" in md


def test_terminal_renders_steps(repo: Path) -> None:
    from harness.report import render_roadmap_terminal

    step = PlanStep(order=1, title="改退款", done_when="6 条测试全绿",
                    touches=["app/service.py"], verify="pytest -q")
    text = render_roadmap_terminal(_plan_result([step], []), color=False)
    assert "实施计划" in text
    assert "完成条件" in text
