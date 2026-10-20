"""公共工具层（tools_common）的行为测试。

为什么单独立一个文件：今天「可插拔 Agent」改造把 9 个工具抽成 tools_common，
作为 review 与 architect 两个 agent 的**唯一实现**。此前没有任何行为测试
（test_agents 只断言了两个工厂是同一对象）—— 这里任何一处回归会同时打坏
两个助手，而 130 条绿测试一条都拦不住。这个文件补上最小行为覆盖。

覆盖原则：每个工具只测「契约级」行为 —— 失败信封的类型、错误信息里的
修复指引、边界的拒绝。具体业务逻辑（如正则引擎）不在这里重复测。
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from harness.sandbox import CommandRunner, Workspace  # noqa: E402
from harness.tools_design import build_design_toolbox  # noqa: E402
from harness.types import RunState  # noqa: E402

@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个最小工作区：一个包、一个密钥文件、一个带缺陷行的源文件。"""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "app.py").write_text(
        "import os\n\n\ndef run(cmd):\n    return eval(cmd)\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-test-not-real\n", encoding="utf-8")
    return tmp_path

@pytest.fixture
def registry(repo: Path):
    """用架构工具箱当载体：它持有全部 9 个公共工具 + 4 个产出工具，
    是 tools_common 的真实消费者之一。"""
    workspace = Workspace(repo)
    runner = CommandRunner(workspace=workspace, allow=(), timeout=5)
    return build_design_toolbox(workspace, RunState(goal="g"), runner)

def _call(registry, tool: str, **kwargs):
    return registry.get(tool).handler(**kwargs).render()

# =============================================================================
# list_files
# =============================================================================

def test_list_files_reports_missing_directory_instead_of_empty(registry) -> None:
    """目录不存在必须显式失败，不能伪装成「没有匹配内容」。

    早先 iter_files 对不存在的路径静默返回空，于是模型拼错目录会得到
    「没有文件」—— 审计场景里这直接驱动假阴性结论。
    """
    out = _call(registry, "list_files", path="pkg/typo")
    assert "目录不存在" in out
    assert "list_files" in out  # 错误信封带修复指引

def test_list_files_rejects_escape(registry) -> None:
    out = _call(registry, "list_files", path="../outside")
    assert "PathEscape" in out or "工作区" in out

def test_list_files_lists_real_files(registry) -> None:
    out = _call(registry, "list_files", path="pkg", pattern="*.py")
    assert "app.py" in out

# =============================================================================
# read_file
# =============================================================================

def test_read_file_blocks_secret_files_at_the_tool_layer(registry) -> None:
    """密钥文件的护栏在工具层，不在 HTTP 层 —— Agent 的读结果会进模型上下文。"""
    out = _call(registry, "read_file", path=".env")
    assert "SecretFileBlocked" in out or "密钥" in out or "sk-" not in out

def test_read_file_paginates_with_offset_and_limit(registry) -> None:
    out = _call(registry, "read_file", path="pkg/app.py", offset=1, limit=2)
    assert "import os" in out
    assert "return eval" not in out, "limit=2 时不应出现第 5 行"

def test_read_file_flags_offset_past_end(registry) -> None:
    """offset 超过文件行数时要明说，不能返回「第 9999-76 行」这种自相矛盾的文本。"""
    out = _call(registry, "read_file", path="pkg/app.py", offset=9999)
    assert "超出" in out or "不存在" in out

# =============================================================================
# grep
# =============================================================================

def test_grep_rejects_broken_regex_with_actionable_message(registry) -> None:
    out = _call(registry, "grep", pattern="eval(")
    assert "InvalidRegex" in out
    assert "转义" in out  # 修复指引

def test_grep_reports_missing_directory(registry) -> None:
    out = _call(registry, "grep", pattern="x", path="nope")
    assert "目录不存在" in out

def test_grep_finds_content(registry) -> None:
    out = _call(registry, "grep", pattern=r"eval\(", path="pkg")
    assert "app.py:5" in out

# =============================================================================
# update_todo
# =============================================================================

def test_update_todo_rejects_unknown_status(registry) -> None:
    out = _call(registry, "update_todo", todos=[{"text": "读变更", "status": "done"}])
    assert "status" in out and ("pending" in out or "completed" in out)

def test_update_todo_replaces_the_list(registry) -> None:
    _call(registry, "update_todo", todos=[{"text": "第一步", "status": "completed"}])
    out = _call(registry, "update_todo", todos=[{"text": "第二步", "status": "in_progress"}])
    assert "第二步" in out
    assert "第一步" not in out, "整个列表替换，旧条目不该残留"

# =============================================================================
# run_command
# =============================================================================

def test_run_command_rejects_non_argv(repo) -> None:
    """架构工具箱刻意不含 run_command（只读助手），这里用审查工具箱验证行为。"""
    from harness.tools_audit import build_toolbox

    workspace = Workspace(repo)
    runner = CommandRunner(workspace=workspace, allow=(), timeout=5)
    audit = build_toolbox(workspace, RunState(goal="g"), runner).registry
    out = _call(audit, "run_command", argv="git status")
    assert "argv" in out and "数组" in out

def test_run_command_rejects_commands_outside_the_allowlist(repo) -> None:
    """白名单外的命令必须失败；allow=() 时连 git 都不放行。"""
    from harness.tools_audit import build_toolbox

    workspace = Workspace(repo)
    runner = CommandRunner(workspace=workspace, allow=(), timeout=5)
    audit = build_toolbox(workspace, RunState(goal="g"), runner).registry
    out = _call(audit, "run_command", argv=["git", "status"])
    assert "白名单" in out or "不允许" in out

# =============================================================================
# read_skill
# =============================================================================

def test_read_skill_reports_available_docs(registry) -> None:
    out = _call(registry, "read_skill", name="definitely-not-a-skill")
    assert "SkillNotFound" in out

def test_read_skill_rejects_absolute_paths(registry) -> None:
    """技能名以 / 开头时 pathlib 会顶掉 skills_root —— POSIX 下可读任意文件。"""
    out = _call(registry, "read_skill", name="/etc/passwd")
    assert "SkillNotFound" in out
    assert "root:" not in out

# =============================================================================
# 两个 agent 共享同一实现
# =============================================================================

def test_both_agents_get_the_same_common_tools() -> None:
    """公共工具只有一处实现：两个 agent 的工厂返回同一批工具对象。"""
    from harness.tools_audit import build_toolbox

    repo = BACKEND / "examples" / "s1-obvious-danger" / "repo"
    workspace = Workspace(repo)
    runner = CommandRunner(workspace=workspace, allow=(), timeout=5)
    state = RunState(goal="g")

    audit = build_toolbox(workspace, state, runner).registry
    design = build_design_toolbox(workspace, state, runner)
    for name in ("list_files", "read_file", "grep", "finish", "update_todo", "read_symbols"):
        # 两个工具箱各自调工厂建实例，对象自然不同；要一致的是**定义**
        # （schema 含 name/description/parameters，任一边悄悄改描述都会被抓住）。
        assert audit.get(name).schema() == design.get(name).schema(), f"{name} 的定义分叉了"

# =============================================================================
# revive_artifacts：重放终态的全桶还原
# =============================================================================

def test_revive_artifacts_round_trips_every_bucket() -> None:
    """「重放丢一半产出」的唯一防线：每个桶都要能还原，未知类型原样返回。"""
    from harness.types import (
        Component,
        DecisionRecord,
        Finding,
        Layer,
        OpenQuestion,
        revive_artifact,
        revive_artifacts,
    )

    finding = revive_artifact(
        "finding",
        {"severity": "blocker", "file": "a.py", "line": 3, "title": "t", "detail": "d", "evidence": "e"},
    )
    assert isinstance(finding, Finding) and finding.line == 3

    component = revive_artifact(
        "component",
        {"name": "svc", "layer": "L", "depends_on": ["db"], "unknown_future_field": 1},
    )
    assert isinstance(component, Component) and component.depends_on == ["db"]
    assert not hasattr(component, "unknown_future_field"), "未知字段应被过滤"

    layer = revive_artifact("layer", {"name": "L", "purpose": "p"})
    decision = revive_artifact("decision", {"title": "d", "decision": "x"})
    question = revive_artifact("open_question", {"question": "q", "blocking": True})
    assert isinstance(layer, Layer)
    assert isinstance(decision, DecisionRecord)
    assert isinstance(question, OpenQuestion) and question.blocking is True

    # 未知 kind：原样返回 dict（前端有兜底渲染器消费它）
    passthrough = revive_artifact("spreadsheet", {"cells": 42})
    assert passthrough == {"cells": 42}

    restored = revive_artifacts(
        {"finding": [{"severity": "note", "file": "b.py", "line": 1, "title": "t", "detail": "d", "evidence": "e"}],
         "component": [{"name": "svc"}]}
    )
    assert set(restored) == {"finding", "component"}
    assert len(restored["finding"]) == 1 and len(restored["component"]) == 1

# =============================================================================
# SSE 流端点：实时模式的主干路径
# =============================================================================

def _read_stream(base: str, path: str, stop_after: str = "close") -> list[str]:
    """读 SSE 流直到收到指定事件类型，返回原始帧文本列表。"""
    frames: list[str] = []
    with urllib.request.urlopen(base + path, timeout=30) as resp:
        buffer = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buffer += chunk
            while b"\n\n" in buffer:
                raw, buffer = buffer.split(b"\n\n", 1)
                text = raw.decode("utf-8")
                frames.append(text)
                if f"event: {stop_after}" in text:
                    return frames
    return frames

@pytest.fixture(scope="module")
def sse_server(tmp_path_factory):
    runs_dir = tmp_path_factory.mktemp("runs")
    from http.server import ThreadingHTTPServer

    from harness import server

    original = server.REGISTRY.runs_dir
    server.REGISTRY.runs_dir = runs_dir
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.REGISTRY.runs_dir = original

def _start_offline_run(base: str) -> str:
    body = {
        "repo": "backend/examples/s1-obvious-danger/repo",
        "goal": "检查资源释放",
        "offline": True,
    }
    req = urllib.request.Request(
        base + "/api/audit",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))["run_id"]

def test_sse_stream_replays_history_then_closes(sse_server: str) -> None:
    """SSE 必须先补发历史事件，终态后再推实时帧，最后发 close。

    前端「打开一条已完成的会话」与「接一条还在跑的」走同一条路径，
    这个契约坏了两边同时坏。
    """
    run_id = _start_offline_run(sse_server)

    # 等运行结束（离线模式几秒内完成）
    deadline = threading.Event()
    for _ in range(200):
        with urllib.request.urlopen(f"{sse_server}/api/audit/{run_id}", timeout=15) as resp:
            record = json.loads(resp.read().decode("utf-8"))
        if record.get("status") in ("finished", "error", "stopped"):
            break
        threading.Event().wait(0.2)
    else:
        pytest.fail("离线运行未在预期时间内结束")

    frames = _read_stream(sse_server, f"/api/audit/{run_id}/stream")
    joined = "\n".join(frames)
    assert "event: run" in joined, "第一帧应是 run（带 outcome，自证产出形态）"
    assert '"outcome"' in joined, "run 事件必须带 outcome"
    assert "event: done" in joined, "终态事件必须在流里"
    assert any("event: close" in f for f in frames), "终态后必须发 close 让客户端收尾"
    # 数据帧必须带 seq（客户端断线后靠 Last-Event-ID 补发）；
    # close / ping 是协议帧，本来就不带 seq。
    data_frames = [f for f in frames if "event: close" not in f and ": ping" not in f]
    assert data_frames and all(f.startswith("id: ") for f in data_frames), (
        "数据帧都要带 seq 供断线补发"
    )

def test_sse_stream_supports_since_resume(sse_server: str) -> None:
    """since 参数只补发之后的帧 —— 断线重连的契约。"""
    run_id = _start_offline_run(sse_server)
    for _ in range(200):
        with urllib.request.urlopen(f"{sse_server}/api/audit/{run_id}", timeout=15) as resp:
            record = json.loads(resp.read().decode("utf-8"))
        if record.get("status") in ("finished", "error", "stopped"):
            break
        threading.Event().wait(0.2)

    frames = _read_stream(sse_server, f"/api/audit/{run_id}/stream?since=9999")
    assert frames, "since 大于已有事件数时应直接进终态分支，仍要有关流帧"
    assert any("event: close" in f for f in frames)

# =============================================================================
# lint_file 的 import 完整性
# =============================================================================

def test_lint_file_runs_without_nameerror(tmp_path) -> None:
    """lint_file 里用了 ast.parse，但曾经漏了 `import ast`。

    这类错误单元测试最容易漏：只有真跑一次工具才暴露。当时是在一次
    diff 增量审查里被 Agent 撞出来的（它在总结里如实报告了工具失败）。
    """
    from harness.sandbox import CommandRunner
    from harness.tools_audit import _tool_lint_file

    (tmp_path / "ok.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("def f(:\n    pass\n", encoding="utf-8")

    ws = Workspace(tmp_path)
    runner = CommandRunner(workspace=ws, allow=[], timeout=10)
    tool = _tool_lint_file(ws, runner)

    good = tool.handler(path="ok.py")
    assert good.ok, getattr(good, "error_message", good)

    bad = tool.handler(path="bad.py")
    # 语法错误应当被报告出来，而不是抛 NameError
    text = str(getattr(bad, "output", "") or getattr(bad, "message", ""))
    assert "NameError" not in text
