"""安全狩猎助手（hunter）的行为测试。

守四件事：

1. **攻击路径必须可重放** —— 这是这份产出的可复核性所在。
   「可能存在越权风险」和「遍历 order_id → 拿到他人明细」不是一回事。
2. **不能说清危害就不许报** —— 写不出 impact 通常说明这个「漏洞」不成立。
3. **两个机械清点工具是候选生成器，不是漏洞判定器** —— 输出里必须写明这点，
   否则模型会把「没看到鉴权代码」直接当成「有越权」。
4. **规则能报的不要重复报** —— 否则同一个问题在 PR 上出现两次。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import agents
from harness.sandbox import CommandRunner, Workspace
from harness.tools_hunt import _path_is_concrete, build_hunt_toolbox
from harness.types import THREAT_CATEGORIES, RunState, Threat


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "api.py").write_text(
        "def handle_get_order(request, order_id):\n"
        "    return store[order_id]\n"
        "\n"
        "\n"
        "def handle_purge(request):\n"
        "    require_ops(request)\n"
        "    return purge()\n",
        encoding="utf-8",
    )
    return root


def _tool(repo: Path):
    ws = Workspace(repo)
    state = RunState(goal="g", max_iterations=5)
    state.artifacts = {"threat": []}
    runner = CommandRunner(workspace=ws, allow={"python"}, timeout=30)
    return build_hunt_toolbox(ws, state, runner), state


# =============================================================================
# 产出类型
# =============================================================================


def test_threat_is_registered() -> None:
    from harness.types import ARTIFACT_TYPES

    assert ARTIFACT_TYPES.get("threat") is Threat


def test_threat_categories_cover_the_real_classes() -> None:
    """六类都必须有 —— 它们是靶场 34 条标注的实际分布。"""
    assert set(THREAT_CATEGORIES) == {
        "authz", "race", "boundary", "injection", "crypto", "consistency"
    }


def test_threat_fingerprint_ignores_wording() -> None:
    """同一漏洞用不同话术描述两次，不该算两条。"""
    a = Threat(title="订单详情不校验归属", file="a.py", line=71, category="authz")
    b = Threat(title="订单详情不校验归属", file="a.py", line=71, category="authz")
    c = Threat(title="订单详情不校验归属", file="a.py", line=99, category="authz")
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint


# =============================================================================
# 攻击路径的可重放性
# =============================================================================


@pytest.mark.parametrize("path", [
    "存在越权风险",
    "权限校验不足，可能被利用",
    "可能有安全风险",
    "这里不太对",
])
def test_vague_attack_path_is_detected(path: str) -> None:
    """只有表态、没有动作的写法必须被识别为空话。"""
    assert _path_is_concrete(path) is False


@pytest.mark.parametrize("path", [
    "任意已登录用户遍历 order_id → GET /orders/<id> → 返回他人订单明细",
    "两笔并发退款同时读到 refunded=0 → 各自写回 100 → 实际退了 200 但只记 100",
    "构造 negative quantity 提交 → 库存反向增加",
    "调用 handle_export 并传 owner=他人 → 直接下载他人订单",
])
def test_concrete_attack_path_is_accepted(path: str) -> None:
    assert _path_is_concrete(path) is True


def test_record_rejects_vague_path(repo: Path) -> None:
    tb, _ = _tool(repo)
    r = tb.get("record_threat").handler(
        title="订单详情不校验归属", file="app/api.py", line=1, category="authz",
        attack_path="存在越权风险", impact="可读取他人订单",
    )
    assert r.ok is False
    assert r.error_type == "VagueAttackPath"
    # 建议里要给可重放的示范，否则模型只会改措辞
    assert "遍历" in r.suggestion or "GET" in r.suggestion


def test_record_rejects_short_path(repo: Path) -> None:
    tb, _ = _tool(repo)
    r = tb.get("record_threat").handler(
        title="t", file="app/api.py", line=1, category="authz",
        attack_path="调接口", impact="读到数据",
    )
    assert r.ok is False
    assert r.error_type == "VagueAttackPath"


# =============================================================================
# 必须说清危害
# =============================================================================


def test_record_rejects_missing_impact(repo: Path) -> None:
    """写不出危害，通常说明这个「漏洞」其实不成立。"""
    tb, _ = _tool(repo)
    r = tb.get("record_threat").handler(
        title="订单详情不校验归属", file="app/api.py", line=1, category="authz",
        attack_path="遍历 order_id → GET /orders/<id> → 返回他人明细",
        impact="",
    )
    assert r.ok is False
    assert r.error_type == "MissingImpact"


def test_record_rejects_invalid_category(repo: Path) -> None:
    tb, _ = _tool(repo)
    r = tb.get("record_threat").handler(
        title="t", file="app/api.py", line=1, category="whatever",
        attack_path="遍历 id → 拿到数据", impact="读到他人数据",
    )
    assert r.ok is False
    assert r.error_type == "InvalidCategory"
    assert "authz" in r.suggestion


def test_record_accepts_a_complete_threat(repo: Path) -> None:
    tb, state = _tool(repo)
    r = tb.get("record_threat").handler(
        title="订单详情只按 order_id 取单，不校验归属",
        file="app/api.py", line=1, category="authz",
        actor="任意已登录用户",
        attack_path="遍历 order_id → GET /orders/<id> → 返回他人订单明细",
        impact="可读取全站任意客户的订单、地址与手机号",
        preconditions="order_id 是自增整数，可枚举",
        variants=["app/api.py:5"],
        evidence="app/api.py:1",
    )
    assert r.ok is True
    assert len(state.artifacts_of("threat")) == 1
    saved = state.artifacts_of("threat")[0]
    assert saved.category == "authz"
    assert saved.variants == ["app/api.py:5"]


def test_overlapping_rule_gets_a_hint(repo: Path) -> None:
    """规则已能报的问题不该由狩猎重复报 —— 同一问题在 PR 上出现两次。"""
    tb, _ = _tool(repo)
    r = tb.get("record_threat").handler(
        title="硬编码密钥", file="app/api.py", line=1, category="crypto",
        attack_path="拿到源码 → 读到明文密钥 → 直接使用",
        impact="可冒充服务身份调用下游",
        overlaps_rule="SEC001",
    )
    assert r.ok is True
    assert "SEC001" in (r.data or "")
    assert "不该由狩猎助手报" in (r.data or "") or "去掉" in (r.data or "")


# =============================================================================
# 机械清点：候选生成器，不是漏洞判定器
# =============================================================================


def test_authz_surface_finds_unguarded_entry(repo: Path) -> None:
    tb, _ = _tool(repo)
    text = tb.get("check_authz_surface").handler().render()
    assert "handle_get_order" in text
    # handle_purge 调了 require_ops，应被标为有校验
    assert "handle_purge" in text


def test_authz_surface_declares_it_is_not_a_verdict(repo: Path) -> None:
    """**输出必须写明「这不是越权结论」**。

    不说的话模型会直接把「没看到鉴权代码」当成「有越权」，
    报出一堆评审一看就知道是误报的条目 —— 那比不报更伤信任。
    """
    tb, _ = _tool(repo)
    text = tb.get("check_authz_surface").handler().render()
    assert "不是「越权漏洞」的结论" in text or "不是越权" in text
    assert "更外层" in text or "中间件" in text


def test_authz_surface_flags_same_file_inconsistency(repo: Path) -> None:
    """同文件里有的校验有的没校验 —— 最像漏洞的信号。"""
    tb, _ = _tool(repo)
    text = tb.get("check_authz_surface").handler().render()
    assert "不一致" in text


def test_authz_surface_handles_non_web_repo(tmp_path: Path) -> None:
    """不是 Web 服务时要明说，而不是静默返回空。"""
    (tmp_path / "lib.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    ws = Workspace(tmp_path)
    state = RunState(goal="g"); state.artifacts = {"threat": []}
    tb = build_hunt_toolbox(ws, state, CommandRunner(workspace=ws, allow={"python"}, timeout=30))
    text = tb.get("check_authz_surface").handler().render()
    assert "没有识别到路由" in text
    # 要给替代方案，而不是让模型卡住
    assert "grep" in text


def test_concurrency_surface_flags_read_modify_write(repo: Path) -> None:
    (repo / "app" / "svc.py").write_text(
        "class S:\n"
        "    def refund(self, oid, amt):\n"
        "        self.store[oid] = self.store[oid] - amt\n",
        encoding="utf-8",
    )
    tb, _ = _tool(repo)
    text = tb.get("check_concurrency_surface").handler().render()
    assert "读-改-写" in text or "没有锁" in text
    # 同样要声明这不是判定
    assert "不是「有竞态」" in text or "不是有竞态" in text


# =============================================================================
# Agent 边界
# =============================================================================


def test_hunter_is_registered_with_its_own_outcome() -> None:
    spec = agents.get("hunter")
    assert spec.outcome == "threat_model"
    assert spec.artifact_kinds == ("threat",)


def test_hunter_cannot_edit_code(repo: Path) -> None:
    """狩猎只报告，不改代码。"""
    spec = agents.get("hunter")
    assert spec.can_edit is False

    tb, _ = _tool(repo)
    assert "apply_patch" not in tb.names()


def test_hunter_does_not_own_other_agents_artifacts(repo: Path) -> None:
    """不该有 record_finding / record_test_case / record_plan_step。"""
    tb, _ = _tool(repo)
    names = set(tb.names())
    assert "record_finding" not in names
    assert "record_test_case" not in names
    assert "record_plan_step" not in names
    assert "record_threat" in names


def test_hunter_has_both_surface_tools(repo: Path) -> None:
    tb, _ = _tool(repo)
    assert "check_authz_surface" in tb.names()
    assert "check_concurrency_surface" in tb.names()


def test_all_five_agents_coexist() -> None:
    assert {"review", "architect", "tester", "planner", "hunter"} <= set(agents.ids())
