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
