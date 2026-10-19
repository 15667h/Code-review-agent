"""diff 增量审查测试。

只测「定位」这件事：算 diff、抽符号、反查调用点。
**不做任何判断逻辑的断言** —— 这个模块的职责边界就是「告诉模型哪里变了」，
有没有缺陷由模型判、由判官层复核。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.diffmode import _range_text, collect_diff, parse_unified_diff


def test_parse_marks_only_added_lines() -> None:
    """只认新增行：删除行在当前工作树里已不存在，拿它核对会一律失败。"""
    diff = "\n".join(
        [
            "diff --git a/app/a.py b/app/a.py",
            "--- a/app/a.py",
            "+++ b/app/a.py",
            "@@ -3,2 +3,3 @@",
            " x = 1",
            "+y = 2",
            "-z = 3",
            " w = 4",
        ]
    )
    hunks, _ = parse_unified_diff(diff)
    assert len(hunks) == 1
    assert hunks[0].file == "app/a.py"
    assert hunks[0].added == [4]
    assert hunks[0].removed == [5]


def test_parse_extracts_symbols_and_constants() -> None:
    diff = "\n".join(
        [
            "+++ b/app/a.py",
            "@@ -0,0 +1,4 @@",
            "+def new_helper():",
            "+    pass",
            "+class NewThing:",
            "+MAX_RETRY = 3",
        ]
    )
    _, symbols = parse_unified_diff(diff)
    assert "new_helper" in symbols
    assert "NewThing" in symbols
    assert "MAX_RETRY" in symbols


def test_range_text_collapses() -> None:
    assert _range_text([1, 2, 3, 7, 9, 10]) == "1-3, 7, 9-10"
    assert _range_text([5]) == "5"
    assert _range_text([]) == "—"


def test_rejects_non_git_directory(tmp_path: Path) -> None:
    report = collect_diff(tmp_path, "HEAD")
    assert report.ok is False
    assert "git" in report.error


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="需要 git",
)
def test_end_to_end_finds_callers_outside_diff(tmp_path: Path) -> None:
    """核心价值：改动一个符号时，要把**作者没打开过的调用点**找出来。"""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "service.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "app" / "api.py").write_text(
        "from .service import helper\n\ndef handle():\n    return helper()\n", encoding="utf-8"
    )

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=str(tmp_path), capture_output=True, text=True)

    git("init", "-q")
    git("add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")

    # 改 service.py 里的 helper
    (tmp_path / "app" / "service.py").write_text(
        "def helper(scale=2):\n    return 1 * scale\n", encoding="utf-8"
    )

    report = collect_diff(tmp_path, "HEAD")
    assert report.ok
    assert report.files == ["app/service.py"]
    assert "helper" in report.changed_symbols

    caller_files = {path for path, _text, _no in report.callers}
    assert "app/api.py" in caller_files, "没找到改动之外的调用点"

    brief = report.brief()
    assert "只审查" in brief
    assert "app/api.py" in brief
