"""可插拔 Agent 的回归测试。

覆盖的是「加第二个 agent 时才可能坏掉」的地方：

- 产出容器是否真的通用（而不是只把 finding 换了个名字）；
- agent 声明是否真的决定了提示词、工具集与产出桶；
- 架构助手那条「只设计、不改代码」的边界有没有被执行；
- 新增的 record_* 工具会不会被熔断牵连（这是真实踩过的坑）；
- HTTP 层是否把未知 agent 挡在入口。

这些都用真实的工具箱与真实的循环跑，不 mock —— 否则测的是测试自己。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from harness import agents  # noqa: E402
from harness.config import Config  # noqa: E402
from harness.guardrails import is_circuit_exempt  # noqa: E402
from harness.loop import run_agent, resolve_workspace  # noqa: E402
from harness.model import ScriptedModel  # noqa: E402
from harness.offline import HeuristicModel  # noqa: E402
from harness.sandbox import CommandRunner, Workspace  # noqa: E402
from harness.tools import ToolRegistry  # noqa: E402
from harness.types import (  # noqa: E402
    Component,
    DecisionRecord,
    Finding,
    Layer,
    OpenQuestion,
    RunState,
    ToolCall,
)

SAMPLE_REPO = BACKEND / "examples" / "s3-input-paths" / "repo"


def _runner(workspace: Workspace) -> CommandRunner:
    return CommandRunner(workspace=workspace, allow=(), timeout=5)


def _registry(agent_id: str, state: RunState | None = None) -> ToolRegistry:
    spec = agents.get(agent_id)
    workspace = Workspace(SAMPLE_REPO)
    return spec.build_tools(workspace, state or RunState(goal="g"), _runner(workspace))


# =============================================================================
# 产出容器
# =============================================================================


def test_artifacts_are_bucketed_by_kind() -> None:
    state = RunState(goal="g")
    state.artifacts = {kind: [] for kind in ("layer", "component")}

    assert state.add_artifact("layer", Layer(name="接入层")) is True
    assert state.add_artifact("layer", Layer(name="接入层")) is False, "同名分层应被去重"
    assert state.artifacts_of("layer")[0].name == "接入层"
    assert state.artifact_counts() == {"layer": 1, "component": 0}


def test_findings_is_a_view_over_the_finding_bucket() -> None:
    """findings 必须与 artifacts["finding"] 是同一份数据。

    分成两处就等于有了两个真相，报告与状态栏早晚会对不上。
    """
    state = RunState(goal="g")
    finding = Finding(severity="warn", file="a.py", line=1, title="t", detail="d", evidence="e")
    state.add_finding(finding)
    assert state.findings == [finding]
    assert state.artifacts["finding"] is state.findings


def test_artifacts_without_fingerprint_are_not_deduped() -> None:
    state = RunState(goal="g")
    payload = OpenQuestion(question="同一个问题？")
    payload.fingerprint = ""
    assert state.add_artifact("open_question", payload) is True
    assert state.add_artifact("open_question", OpenQuestion(question="X", fingerprint="")) is True


# =============================================================================
# 注册表
# =============================================================================


def test_registry_exposes_both_agents() -> None:
    ids = agents.ids()
    assert "review" in ids and "architect" in ids
    assert agents.default_id() == "review"


def test_manifest_has_everything_the_frontend_needs() -> None:
    """前端的助手轨道完全由这份清单渲染，缺一个字段就得改前端代码。"""
    for item in agents.manifests():
        for key in ("id", "name", "tagline", "accent", "room", "outcome", "artifact_kinds"):
            assert key in item, f"{item.get('id')} 缺少 {key}"
        assert item["accent"].startswith("#")


def test_unknown_agent_raises_with_available_ids() -> None:
    with pytest.raises(agents.UnknownAgent) as excinfo:
        agents.get("nope")
    assert "review" in str(excinfo.value), "错误信息要列出可用的 agent"


def test_registering_a_duplicate_id_is_rejected() -> None:
    spec = agents.AgentSpec(
        id="review",
        name="重名",
        tagline="x",
        accent="#000000",
        room="x",
        system_prompt="x",
        goal_heading="x",
        default_goal="x",
        outcome="findings",
        artifact_kinds=("finding",),
        build_tools=lambda *_: ToolRegistry(),
    )
    with pytest.raises(ValueError, match="已注册"):
        agents.register(spec)


def test_adding_a_third_agent_needs_only_a_spec() -> None:
    """可插拔的验收标准：新 agent 不改动任何既有代码。"""
    spec = agents.AgentSpec(
        id="probe",
        name="探针",
        tagline="只验证可插拔是否成立",
        accent="#663399",
        room="试验台",
        system_prompt="你是探针。",
        goal_heading="探测任务",
        default_goal="探测。",
        outcome="notes",
        artifact_kinds=("note",),
        build_tools=lambda *_: ToolRegistry(),
    )
    agents.register(spec)
    try:
        assert agents.has("probe")
        assert spec.manifest()["accent"] == "#663399"
    finally:
        agents._SPECS.pop("probe", None)
    assert not agents.has("probe")


# =============================================================================
# 工具集边界
# =============================================================================


def test_architect_cannot_edit_code() -> None:
    """架构助手不给写工具 —— 设计与实施混在一起就分不清哪些是现状。"""
    review = _registry("review")
    design = _registry("architect")

    assert review.get("apply_patch") is not None
    assert design.get("apply_patch") is None
    assert design.get("run_tests") is None
    assert design.get("lint_file") is None
    assert agents.get("architect").can_edit is False


def test_architect_has_its_four_output_tools_as_core() -> None:
    """产出工具必须在静态前缀里：否则模型会攒到最后一次性输出，中间过程就丢了。"""
    design = _registry("architect")
    for name in ("record_layer", "record_component", "record_decision", "record_open_question"):
        tool = design.get(name)
        assert tool is not None, f"缺少 {name}"
        assert design.is_core(name), f"{name} 应在静态前缀里"


def test_both_agents_share_the_same_file_tools() -> None:
    """公共工具只能有一处实现。

    曾经两处各写一套过滤规则，同一个仓库一个显示 61 个文件、一个列出 71 个。
    这里同时验证两件事：工厂函数是同一个（一处实现），
    以及两边拿到的 schema 完全一致（不会有一边悄悄改了描述）。
    """
    from harness import tools_audit, tools_common, tools_design

    assert tools_audit._tool_read_file is tools_common._tool_read_file
    assert tools_design._tool_read_file is tools_common._tool_read_file
    assert tools_audit._tool_grep is tools_design._tool_grep

    review = _registry("review")
    design = _registry("architect")
    for name in ("read_file", "grep", "list_files", "update_todo", "finish"):
        assert review.get(name).schema() == design.get(name).schema(), f"{name} 的定义分叉了"


def test_new_record_tools_are_circuit_exempt() -> None:
    """熔断期间产出工具必须仍然可用。

    这是真实踩过的坑：模型连试几次高风险工具后，14 条发现一条都没能落库。
    名单用前缀判定，加 agent 时不需要记得同步改这里。
    """
    for name in ("record_finding", "record_layer", "record_component", "record_decision"):
        assert is_circuit_exempt(name), f"{name} 必须豁免"
    assert is_circuit_exempt("finish")
    assert is_circuit_exempt("update_todo")
    assert not is_circuit_exempt("run_command")
    assert not is_circuit_exempt("apply_patch")


# =============================================================================
# 产出工具的行为
# =============================================================================


def test_record_component_warns_about_unregistered_dependencies() -> None:
    """依赖指向未登记的组件会让图上少一条边，这种缺失很难事后发现。"""
    state = RunState(goal="g")
    registry = _registry("architect", state)
    result = registry.execute(
        ToolCall("record_component", {"name": "order", "depends_on": ["ghost"]})
    )
    assert result.ok
    assert "ghost" in str(result.data), "应当提醒依赖未登记"


def test_record_tools_dedupe_by_name() -> None:
    state = RunState(goal="g")
    registry = _registry("architect", state)
    registry.execute(ToolCall("record_layer", {"name": "接入层"}))
    second = registry.execute(ToolCall("record_layer", {"name": "接入层"}))
    assert "已存在" in str(second.data)
    assert state.artifact_counts()["layer"] == 1


def test_component_and_decision_serialize_for_the_frontend() -> None:
    component = Component(name="order", layer="领域层", depends_on=["mysql"])
    assert component.to_dict()["depends_on"] == ["mysql"]
    assert DecisionRecord(title="t", alternatives=["a"]).to_dict()["alternatives"] == ["a"]
    assert OpenQuestion(question="q", blocking=True).to_dict()["blocking"] is True
    assert Layer(name="接入层", order=0).to_dict()["order"] == 0


# =============================================================================
# 主循环按 agent 分派
# =============================================================================


def test_run_agent_uses_the_declared_prompt_and_tools() -> None:
    """用脚本模型跑一回合，看它拿到的到底是什么。"""
    seen: dict[str, object] = {}

    class Probe(ScriptedModel):
        pass

    workspace = Workspace(SAMPLE_REPO)
    config = Config()
    config.max_iterations = 1

    captured_tools: list[str] = []

    def spy(messages, tools=None, temperature=None):
        seen["system"] = messages[0].content
        seen["first_user"] = messages[1].content if len(messages) > 1 else ""
        captured_tools.extend(t["function"]["name"] for t in (tools or []))
        from harness.types import Decision

        return Decision(content="完成", reasoning="")

    model = Probe([])
    model.complete = spy  # type: ignore[method-assign]

    result = run_agent(goal="给个方案", workspace=workspace, model=model, config=config, agent="architect")

    assert "架构设计助手" in str(seen["system"]), "应当用架构助手的提示词"
    assert "# 设计任务" in str(seen["first_user"]), "首条消息的标题应由 agent 声明"
    assert "record_component" in captured_tools
    assert "apply_patch" not in captured_tools
    assert result.agent == "architect"
    assert result.outcome == "blueprint"
    assert set(result.artifacts) == {"layer", "component", "decision", "open_question"}


def test_run_agent_rejects_unknown_ids() -> None:
    workspace = Workspace(SAMPLE_REPO)
    with pytest.raises(agents.UnknownAgent):
        run_agent(
            goal="x",
            workspace=workspace,
            model=ScriptedModel([]),
            config=Config(),
            agent="nope",
        )


def test_run_audit_still_routes_to_review() -> None:
    """既有入口的名字与行为都不变，CLI 与评估脚本不该被波及。"""
    from harness.loop import run_audit

    workspace = Workspace(SAMPLE_REPO)
    config = Config()
    config.max_iterations = 1
    result = run_audit(
        goal="x", workspace=workspace, model=ScriptedModel([]), config=config
    )
    assert result.agent == "review"
    assert result.outcome == "findings"


def test_offline_architect_run_produces_a_blueprint() -> None:
    """离线规则模式的架构链路：真的调用工具、真的产出层与组件。"""
    workspace = resolve_workspace(str(SAMPLE_REPO))
    config = Config()
    result = run_agent(
        goal="盘一下结构",
        workspace=workspace,
        model=HeuristicModel(agent="architect"),
        config=config,
        agent="architect",
    )
    assert result.stop_reason == "finished"
    assert result.artifact_counts["layer"] >= 1
    assert result.artifact_counts["component"] >= 1
    # 规则模式不做设计判断，所以它必须把「这样切对不对」留成开放问题
    assert result.artifact_counts["open_question"] >= 1
    # 架构助手连 finding 这个桶都不该有 —— 只是「读一眼有没有发现」
    # 就建出空桶的话，运行记录里会多出一条与本次任务无关的 finding: 0
    assert "finding" not in result.artifact_counts, "架构助手不该产出发现，也不该留下空桶"
    assert result.findings == []


def test_offline_review_run_is_unchanged() -> None:
    workspace = resolve_workspace(str(BACKEND / "examples" / "vulnerable-service" / "repo"))
    config = Config()
    result = run_agent(
        goal="找出缺陷",
        workspace=workspace,
        model=HeuristicModel(),
        config=config,
        agent="review",
    )
    assert result.artifact_counts["finding"] > 0
    assert result.artifact_counts.get("component", 0) == 0, "审查助手不该产出组件"


# =============================================================================
# 状态栏
# =============================================================================


def test_status_bar_reports_whatever_the_agent_produces() -> None:
    """状态栏不能写死 findings：架构助手会被误导成「自己什么都没做」。"""
    from harness.context import render_status

    state = RunState(goal="g")
    state.artifacts = {"layer": [], "component": []}
    state.add_artifact("component", Component(name="order"))
    text = render_status(state)
    assert "component=1" in text
    assert "layer=0" in text, "空桶也要报数：评审要看清「有几层、几个组件」"
    assert "findings" not in text, "形态不是 findings 时不该出现 findings 字样"

    review_state = RunState(goal="g")
    review_state.add_finding(
        Finding(severity="blocker", file="a.py", line=1, title="t", detail="d", evidence="e")
    )
    review_text = render_status(review_state)
    assert "finding=1" in review_text


def test_status_extra_lines_come_from_the_agent() -> None:
    from harness.context import render_status

    state = RunState(goal="g")
    state.artifacts = {"open_question": []}
    state.add_artifact("open_question", OpenQuestion(question="q", blocking=True))
    text = render_status(state, extra_lines=agents.get("architect").status_lines(state))
    assert "blocking=1" in text


def test_finish_tally_follows_the_outcome() -> None:
    """收尾统计必须与产出形态一致：架构助手不该看到「共 0 条发现」。

    这句话会进模型上下文，也会进运行记录 —— 说得不对就是拿一个
    与本次任务无关的指标去误导人和模型。
    """
    from harness.types import Layer

    arch_state = RunState(goal="g")
    arch_state.artifacts = {"layer": []}
    arch_state.add_artifact("layer", Layer(name="接入层"))
    arch_out = str(_registry("architect", arch_state).get("finish").handler(summary="done").data)
    assert "条发现" not in arch_out
    assert "已登记" in arch_out and "1层" in arch_out.replace(" ", "")

    review_state = RunState(goal="g")
    review_state.add_finding(
        Finding(severity="blocker", file="a.py", line=1, title="t", detail="d", evidence="e")
    )
    review_out = str(_registry("review", review_state).get("finish").handler(summary="done").data)
    assert "共 1 条发现" in review_out
    assert "阻断 1" in review_out


# =============================================================================
# 工具参数的健壮性（2026-10-16 实测暴露：模型连续把单项数组写成裸字符串）
# =============================================================================


def _array_params(registry: ToolRegistry):
    """产出 (工具名, 参数名, 描述) —— 所有 array 参数。"""
    for tool in registry.all():
        for key, spec in (tool.parameters.get("properties") or {}).items():
            if spec.get("type") == "array":
                yield tool.name, key, spec.get("description") or ""


def test_every_array_param_declares_itself_as_array() -> None:
    """每个 array 参数的描述必须自证「这是数组，每项一个…」。

    实测：模型最常把「只有一个条目的数组」写成裸字符串，错误只说
    「应为 array」时它下一轮还猜。描述里给出目标形态后，同类失败从 7 次降到 0 次 ——
    但只修了当时暴露的那个参数（failure_modes）， alternatives 和 todos 是漏网的。
    这条测试让「新增 array 参数忘记写描述」立刻红灯，不再靠实测发现。
    """
    checked = 0
    for agent_id in ("review", "architect"):
        for tool_name, key, desc in _array_params(_registry(agent_id)):
            checked += 1
            assert "数组" in desc, f"{agent_id}/{tool_name}.{key} 的描述没说这是数组：{desc[:60]!r}"
    assert checked >= 6, f"只覆盖到 {checked} 个 array 参数，检查本身可能失效了"


def test_array_type_error_tells_the_model_what_to_write() -> None:
    """类型错误的文案要给出目标形态，而不是只说「应为 array」。"""
    from harness.tools import validate_arguments

    errors = validate_arguments(
        {"type": "object", "properties": {"alternatives": {"type": "array"}}},
        {"alternatives": "Seata AT 模式：协调成本高"},
    )
    assert errors, "传字符串给 array 参数必须被拒绝（不静默转换）"
    joined = "；".join(errors)
    assert "['...']" in joined, f"错误信息没给出目标形态：{joined}"


def test_record_component_aligns_dependency_names() -> None:
    """依赖名按「忽略大小写与首尾空格」对齐到已登记组件的规范名。

    实测：模型写 'Session' 与 'session' 两种形式，前端按归一找节点，
    名字漂移的边就静默消失 —— 图上少一条边，事后极难发现。
    """
    state = RunState(goal="g")
    registry = _registry("architect", state)

    out = registry.get("record_component").handler(name="Session", layer="会话层").render()
    assert "已记录组件" in out

    # 大小写与首尾空格不同的依赖名，必须被改写成规范名
    registry.get("record_component").handler(
        name="HTTPAdapter", layer="传输层", depends_on=[" session "]
    )
    adapter = state.artifacts_of("component")[-1]
    assert adapter.depends_on == ["Session"], adapter.depends_on

    # 还没登记的依赖保持原样，但回复里必须点名
    out = registry.get("record_component").handler(
        name="Auth", layer="扩展层", depends_on=["credential-store"]
    ).render()
    auth = state.artifacts_of("component")[-1]
    assert auth.depends_on == ["credential-store"], auth.depends_on
    assert "credential-store" in out and "尚未登记" in out


def test_record_component_drops_self_dependency() -> None:
    """自依赖没有信息量，还会画出一条自己指向自己的怪边。

    实测 E2（requests）：'urllib3 PoolManager' 的 depends_on 写了自己，
    旧代码把它当「已知组件」放过去了。
    """
    state = RunState(goal="g")
    registry = _registry("architect", state)

    registry.get("record_component").handler(name="PoolManager", layer="传输层")
    out = registry.get("record_component").handler(
        name="PoolManager", layer="传输层", depends_on=["PoolManager"]
    ).render()
    assert "已记录组件" not in out, "同名组件应判重（这条走判重路径）"

    registry.get("record_component").handler(name="pool", layer="传输层", depends_on=["POOL"])
    pool = state.artifacts_of("component")[-1]
    assert pool.name == "pool" and pool.depends_on == [], pool.depends_on


def test_unknown_outcome_falls_back_to_generic_renderer() -> None:
    """第三个 agent 的 outcome 没有专用渲染器时，必须走 generic 兜底。

    掉进审查渲染器会打印「本次没有记录任何发现」—— 那不是事实，
    是误导，比不渲染更糟。注册表 + 兜底就是为消灭这条路径。
    """
    from harness.loop import AuditResult
    from harness.report import render_generic_markdown, render_markdown, render_terminal

    result = AuditResult(
        goal="盘一遍依赖",
        agent="architect",
        outcome="notes",
        artifacts={"note": [{"title": "观察一", "body": "环形依赖"}]},
        summary="已记录 1 条",
    )
    md = render_markdown(result)
    assert "没有记录任何发现" not in md, "未知形态掉进了审查渲染器"
    assert "notes" in md and "环形依赖" in md
    assert "这一形态还没有专用渲染器" in md

    term = render_terminal(result, color=False)
    assert "没有记录任何发现" not in term
    assert "环形依赖" in term

    # generic 渲染器对空产出也要能说话
    empty = AuditResult(goal="g", outcome="notes", artifacts={})
    assert "没有登记任何产出" in render_generic_markdown(empty)


def test_blueprint_outcome_still_uses_its_own_renderer() -> None:
    """注册表不能把已注册的形态也漏到 generic 去。"""
    from harness.loop import AuditResult
    from harness.report import render_markdown

    from harness.types import Layer

    result = AuditResult(
        goal="分层",
        agent="architect",
        outcome="blueprint",
        artifacts={"layer": [Layer(name="接入层", purpose="对外")], "component": []},
    )
    md = render_markdown(result)
    assert "这一形态还没有专用渲染器" not in md
    assert "接入层" in md
