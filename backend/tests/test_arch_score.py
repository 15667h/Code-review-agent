"""架构蓝图量化评分测试。

关键难点是**命名自由**：架构命名天然有自由度，字面比会把正确切分算成
一漏一多 —— 那是度量错误，不是模型能力差。
"""

from __future__ import annotations

from harness.arch_score import score_blueprint


REF = {
    "layers": [{"name": "注册层", "aliases": ["register", "注册"]}],
    "components": [
        {"name": "HookCaller", "aliases": ["hook-caller", "调用器"]},
        {"name": "PluginManager", "aliases": ["plugin-manager", "管理器"]},
    ],
    "edges": [{"from": "PluginManager", "to": "HookCaller"}],
    "decisions": [{"title": "惰性组装", "aliases": ["lazy", "首次调用时组装"]}],
}


def _art(components=None, layers=None, decisions=None) -> dict:
    return {
        "component": [{"name": c, "depends_on": deps} for c, deps in (components or {}).items()],
        "layer": [{"name": x} for x in (layers or [])],
        "decision": [{"title": x} for x in (decisions or [])],
    }


def test_exact_name_matches() -> None:
    r = score_blueprint(_art({"HookCaller": [], "PluginManager": []}), REF)
    assert r["components"]["matched"] == 2
    assert r["components"]["f1"] == 100.0


def test_naming_variants_match() -> None:
    """hook-caller / 调用器 都是同一个东西，必须算命中。"""
    r = score_blueprint(_art({"hook-caller": [], "管理器": []}), REF)
    assert r["components"]["matched"] == 2, r["components"]["hits"]


def test_composite_name_matches() -> None:
    """模型常写「Hook 调用器（HookCaller）」这种复合名。"""
    r = score_blueprint(_art({"Hook 调用器（HookCaller）": []}), REF)
    assert r["components"]["matched"] == 1


def test_extra_component_lowers_precision() -> None:
    r = score_blueprint(_art({"HookCaller": [], "PluginManager": [], "幽灵组件": []}), REF)
    assert r["components"]["recall"] == 100.0
    assert r["components"]["precision"] < 100.0
    assert "幽灵组件" in r["components"]["extra"]


def test_edge_matching_is_name_normalized() -> None:
    """边必须在同一套归一名字上比，否则命名一自由就全错。"""
    r = score_blueprint(
        _art({"plugin-manager": ["hook-caller"], "HookCaller": []}), REF
    )
    assert r["edges"]["matched"] == 1


def test_decision_uses_title_field() -> None:
    r = score_blueprint(_art(decisions=["惰性组装（首次调用）"]), REF)
    assert r["decisions"]["matched"] == 1


def test_trap_component_is_flagged() -> None:
    """照妖镜：多出的那个「不该存在」的组件要被单独点出来。

    这类错误 P/R 抓不住重点 —— 多报只让精确率降一点，
    但它恰恰是「背架构图而不是读代码」的铁证。
    """
    ref = dict(REF)
    ref["must_not_contain"] = {"components": ["重试层"], "why": "重试在 urllib3"}
    r = score_blueprint(_art({"HookCaller": [], "重试层": []}), ref)
    assert r["violations"] == ["重试层"]
    assert "urllib3" in r["violation_note"]


def test_no_violation_when_clean() -> None:
    ref = dict(REF)
    ref["must_not_contain"] = {"components": ["重试层"]}
    r = score_blueprint(_art({"HookCaller": []}), ref)
    assert r["violations"] == []


def test_empty_blueprint_scores_zero() -> None:
    r = score_blueprint({}, REF)
    assert r["overall"] == 0.0
