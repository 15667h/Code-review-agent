"""架构产出证据核验测试。

判据与审查那边不同：架构说「这个层/组件存在于这里」，
不需要缺陷关键词，只需要文件存在 + 行号在范围内 + 指向代码而非空行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import arch_check


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "core.py").write_text(
        "# 模块说明\n\nclass Engine:\n    def run(self):\n        return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "other.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "src" / "dup.py").write_text("Y = 1\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "dup.py").write_text("Z = 1\n", encoding="utf-8")
    return tmp_path


def test_parse_location() -> None:
    assert arch_check.parse_location("src/a.py:12") == ("src/a.py", 12, 12)
    assert arch_check.parse_location("src/a.py:12-20") == ("src/a.py", 12, 20)
    assert arch_check.parse_location("不是位置") is None


def test_ok_when_path_and_line_valid(root: Path) -> None:
    status, _ = arch_check.check_evidence("src/core.py:3", root)
    assert status == "ok"


def test_bad_when_file_missing(root: Path) -> None:
    status, why = arch_check.check_evidence("src/ghost.py:1", root)
    assert status == "bad"
    assert "不存在" in why


def test_bad_when_line_out_of_range(root: Path) -> None:
    status, why = arch_check.check_evidence("src/core.py:999", root)
    assert status == "bad"
    assert "越界" in why


def test_weak_for_bare_filename_when_unique(root: Path) -> None:
    """只写文件名：能确定目标时算 weak（提示补全），不是编造。"""
    status, why = arch_check.check_evidence(
        "core.py:3", root, unique_basenames={"core.py", "other.py"}
    )
    assert status == "weak"
    assert "文件名" in why


def test_bad_for_ambiguous_bare_filename(root: Path) -> None:
    """同名文件存在两个 → 无法定位，判 bad。"""
    status, _ = arch_check.check_evidence("dup.py:1", root, unique_basenames={"core.py", "other.py"})
    assert status == "bad"


def test_weak_for_comment_only_line(root: Path) -> None:
    status, why = arch_check.check_evidence("src/core.py:1", root)
    assert status == "weak"
    assert "注释" in why


def test_rejects_path_escape(root: Path) -> None:
    status, why = arch_check.check_evidence("../../etc/passwd:1", root)
    assert status == "bad"


def test_verify_blueprint_counts_items_without_evidence(root: Path) -> None:
    """「没给证据」与「证据是错的」要分开统计 —— 前者是遗漏，后者是编造。"""
    report = arch_check.verify_blueprint(
        {
            "component": [
                {"name": "有据", "evidence": ["src/core.py:3"]},
                {"name": "无据"},
            ]
        },
        root,
    )
    assert report["items_without_evidence"] == 1
    assert report["ok"] == 1
    assert report["grounded_rate"] == 100.0


def test_verify_blueprint_reports_problems(root: Path) -> None:
    report = arch_check.verify_blueprint(
        {"component": [{"name": "幽灵", "evidence": ["src/nope.py:3"]}]}, root
    )
    assert report["bad"] == 1
    assert any("幽灵" in p["name"] for p in report["problems"])


def test_summary_mentions_specifics(root: Path) -> None:
    report = arch_check.verify_blueprint(
        {"layer": [{"name": "接入层", "evidence": ["src/core.py:999"]}]}, root
    )
    text = " ".join(arch_check.summary_lines(report))
    assert "接入层" in text and "越界" in text


# =============================================================================
# 权衡真实性（P1）—— 这一组全部来自真实产出的修正
# =============================================================================


def test_flags_only_benefits() -> None:
    problems = arch_check.check_decision_quality(
        {"title": "用缓存", "consequences": "性能提升，代码更清晰", "alternatives": ["直查：简单"]}
    )
    assert any("只有一句好话" in p or "没有代价" in p for p in problems)


def test_accepts_concrete_cost_without_buzzword() -> None:
    """真实产出写的代价通常不带「成本」二字，例如「假边」「必须穷举」。"""
    problems = arch_check.check_decision_quality(
        {
            "title": "用 TYPE_CHECKING 单向化循环依赖",
            "consequences": "依赖图里 models → adapters 是一条假边：只在类型检查期存在，运行时不存在",
            "alternatives": ["把构建下沉到独立模块：更干净，但需大范围重构"],
        }
    )
    assert problems == []


def test_requires_at_least_one_alternative_with_merit() -> None:
    """判据是「整条决策至少有一个备选写了优点」，不是每条都要。

    真实场景：一条决策有三个备选，其中一个（回滚方案）确实只有坏处可写，
    但它旁边的备选写了「实现简单、一遍扫描」—— 这不算敷衍。
    """
    problems = arch_check.check_decision_quality(
        {
            "title": "先全量校验再改状态",
            "consequences": "需要为每个 HookCaller 设计幂等撤销",
            "alternatives": [
                "边发现边挂载：实现简单、一遍扫描，但中途失败会留下部分状态",
                "全部挂载后回滚：需要设计幂等撤销，还要处理不可撤销的副作用",
            ],
        }
    )
    assert problems == []


def test_flags_all_straw_alternatives() -> None:
    problems = arch_check.check_decision_quality(
        {"title": "选 A", "consequences": "增加运维成本", "alternatives": ["方案B：不好", "方案C：没人用"]}
    )
    assert any("全都没写自己的优点" in p for p in problems)


def test_flags_missing_alternatives() -> None:
    problems = arch_check.check_decision_quality(
        {"title": "选 D", "consequences": "增加延迟", "alternatives": []}
    )
    assert any("没有登记任何被放弃的备选" in p for p in problems)


def test_flags_empty_consequences() -> None:
    problems = arch_check.check_decision_quality({"title": "X", "consequences": ""})
    assert any("没有写 consequences" in p for p in problems)
