"""判官层测试。

判官的作用是把「撑不住证据的发现」挡在结果之外，但它**不得产生新发现**。
这里的测试全部用假模型（不调真实 API），钉住三件事：
1. 能删掉「结论与代码相反」的发现；
2. 判官自身失败时**保留全部**（不能把网络抖动放大成「什么都没查到」）；
3. 输出解析要能容忍代码块包裹、前后解释、垃圾文本。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import judge
from harness.model import ModelError
from harness.types import Decision


class FakeModel:
    """按预设返回内容的假模型，记录收到的 prompt 供断言。"""

    name = "fake"

    def __init__(self, reply: str = "", raises: Exception | None = None) -> None:
        self.reply = reply
        self.raises = raises
        self.calls: list[list[object]] = []

    def complete(self, messages, tools=None, temperature=None):
        self.calls.append(list(messages))
        if self.raises is not None:
            raise self.raises
        return Decision(content=self.reply)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pool.py").write_text(
        "\n".join(
            [
                "class Pool:",
                "    def __init__(self):",
                "        self._in_use = 0",
                "",
                "    def acquire(self):",
                "        self._in_use += 1",
                "",
                "    def release(self):",
                "        self._in_use = max(0, self._in_use - 1)",
            ]
        ),
        encoding="utf-8",
    )
    return tmp_path


def _finding(fid: str = "F1", line: int = 9) -> dict:
    return {
        "id": fid,
        "file": "app/pool.py",
        "line": line,
        "severity": "blocker",
        "title": "release 未做下界保护，计数会变负数",
        "detail": "release 直接自减，重复调用会让 _in_use 变成负数。",
        "evidence": "self._in_use -= 1",
    }


def test_drops_ungrounded_finding(root: Path) -> None:
    """判官判 drop → 发现被剔除，并给出理由。"""
    model = FakeModel('{"judgments":[{"id":"F1","verdict":"drop","reason":"已有 max(0,..) 保护"}]}')
    kept, report = judge.judge_findings([_finding()], root, model)
    assert kept == []
    assert len(report.dropped) == 1
    assert "max(0" in report.dropped[0].reason


def test_keeps_grounded_finding(root: Path) -> None:
    model = FakeModel('{"judgments":[{"id":"F1","verdict":"keep"}]}')
    kept, report = judge.judge_findings([_finding()], root, model)
    assert len(kept) == 1
    assert report.dropped == []


def test_model_failure_keeps_everything(root: Path) -> None:
    """判官挂了必须保留全部 —— 它是增强项，不是正确性前提。"""
    model = FakeModel(raises=ModelError("Timeout", "请求超时"))
    kept, report = judge.judge_findings([_finding()], root, model)
    assert len(kept) == 1
    assert "跳过" in report.skipped_reason
    assert "超时" in report.skipped_reason or "Timeout" in report.skipped_reason


def test_unparseable_output_keeps_everything(root: Path) -> None:
    model = FakeModel("我觉得这些发现都挺好的，没有问题。")
    kept, report = judge.judge_findings([_finding()], root, model)
    assert len(kept) == 1
    assert "无法解析" in report.skipped_reason


def test_missing_verdict_defaults_to_keep(root: Path) -> None:
    """模型漏判某条 → 保留（漏判不等于判错）。"""
    model = FakeModel('{"judgments":[{"id":"OTHER","verdict":"drop"}]}')
    kept, report = judge.judge_findings([_finding("F1")], root, model)
    assert len(kept) == 1


def test_position_check_runs_before_judge(root: Path) -> None:
    """位置明显错的发现不进判官 —— 省 token，也避免对着无关代码猜。"""
    model = FakeModel('{"judgments":[{"id":"F1","verdict":"keep"}]}')
    bogus = _finding(line=999)
    kept, report = judge.judge_findings([bogus], root, model)
    assert report.ungrounded == 1
    assert model.calls == [], "位置不可信的发现不该送到模型"


def test_prompt_includes_real_code_with_line_numbers(root: Path) -> None:
    """判官必须看到带行号的真实代码，否则只是在给一段无坐标的文本打分。"""
    model = FakeModel('{"judgments":[{"id":"F1","verdict":"keep"}]}')
    judge.judge_findings([_finding()], root, model)
    user_prompt = model.calls[0][-1].content
    assert "max(0, self._in_use - 1)" in user_prompt
    assert ">>>" in user_prompt and "9 |" in user_prompt.replace(">>>  9", ">>> 9")


def test_prompt_forbids_new_findings(root: Path) -> None:
    """系统提示必须明确禁止提新问题，否则判官变成第二个审查者会重复噪声。"""
    model = FakeModel('{"judgments":[]}')
    judge.judge_findings([_finding()], root, model)
    system_prompt = model.calls[0][0].content
    assert "不要提出新的问题" in system_prompt


def test_empty_input_does_not_call_model(root: Path) -> None:
    model = FakeModel()
    kept, report = judge.judge_findings([], root, model)
    assert kept == [] and report.total == 0
    assert model.calls == []
