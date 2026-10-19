"""架构速写与轮次预算测试。

两块都是「让模型少花轮次」的工程手段：
- sketch：一次机械扫描替代逐文件读（能确定性提取的不交给模型）
- pacing：把剩余轮次写进状态栏（撞上限的根因是模型不知道还剩几轮）
"""

from __future__ import annotations

from pathlib import Path

from harness.pacing import pacing_note, should_warn_finish
from harness.sketch import render, sketch_repo


# --------------------------------------------------------------------------- #
# sketch
# --------------------------------------------------------------------------- #


def test_sketch_extracts_structure(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "core.py").write_text(
        '"""核心模块。"""\n\nimport os\nfrom . import helpers\n\n'
        "class Engine:\n    pass\n\ndef run():\n    pass\n",
        encoding="utf-8",
    )
    sketches = sketch_repo(tmp_path)
    assert len(sketches) == 2

    core = next(s for s in sketches if s.path == "pkg/core.py")
    assert "Engine" in core.classes
    assert "run" in core.functions
    assert core.doc == "核心模块。"
    assert "os" in core.imports


def test_sketch_skips_noise_dirs(tmp_path: Path) -> None:
    for junk in ("node_modules", "__pycache__", ".venv"):
        (tmp_path / junk).mkdir()
        (tmp_path / junk / "x.py").write_text("A = 1\n", encoding="utf-8")
    (tmp_path / "real.py").write_text("B = 1\n", encoding="utf-8")
    assert [s.path for s in sketch_repo(tmp_path)] == ["real.py"]


def test_sketch_skips_unparseable(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def f(:\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("X = 1\n", encoding="utf-8")
    assert [s.path for s in sketch_repo(tmp_path)] == ["ok.py"]


def test_render_says_how_many_hidden(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text(f"V{i} = 1\n", encoding="utf-8")
    text = render(sketch_repo(tmp_path), max_files=2)
    assert "还有 3 个文件未列出" in text


def test_render_handles_empty() -> None:
    assert "没有可解析" in render([])


# --------------------------------------------------------------------------- #
# pacing
# --------------------------------------------------------------------------- #


def test_pacing_reports_progress_at_start() -> None:
    note = pacing_note(0, 14)
    assert "第 1/14 轮" in note
    assert "收尾" not in note  # 刚开始不该催


def test_pacing_asks_to_converge_midway() -> None:
    note = pacing_note(9, 14)  # 剩 4/14 ≈ 29%
    assert "开始收尾" in note


def test_pacing_demands_finish_when_low() -> None:
    note = pacing_note(12, 14)  # 剩 1/14
    assert "立刻收尾" in note
    assert "open_question" in note  # 要求把未覆盖写进开放问题


def test_pacing_uses_ratio_not_absolute() -> None:
    """判据是**剩余比例**，不是剩余绝对轮数。

    剩 4 轮：在 14 轮预算里是 29%（催收敛），在 40 轮预算里是 10%（要求立刻收尾）。
    两种都该有提示，但强度不同 —— 若按绝对轮数判，这两个会被当成同一件事。
    """
    tight = pacing_note(9, 14)    # 剩 4/14 = 29%
    very_low = pacing_note(35, 40)  # 剩 4/40 = 10%
    assert "开始收尾" in tight
    assert "立刻收尾" in very_low  # 同样剩 4 轮，但比例更低 → 更急
    assert "立刻收尾" not in tight


def test_pacing_silent_without_budget() -> None:
    assert pacing_note(0, 0) == ""


def test_should_warn_finish_threshold() -> None:
    assert should_warn_finish(12, 14) is True
    assert should_warn_finish(0, 14) is False
    assert should_warn_finish(5, 0) is False
