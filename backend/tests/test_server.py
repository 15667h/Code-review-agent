"""HTTP 服务层的回归测试。

用真实起服务的集成测试，而不是只测内部函数 —— 这一层的价值全在
「协议对不对、边界挡没挡住」上，绕开 socket 就等于没测。

重点覆盖三类容易悄悄坏掉的东西：
1. **不泄漏**：API Key 只回掩码、`.env` 与后端源码不可通过静态路由取到；
2. **路径契约**：相对路径按项目根解析（服务的 CWD 不该泄漏进接口）；
3. **端到端**：起一次离线审计，能从快照里拿到终态结果。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from harness import server  # noqa: E402
from harness.config import Config  # noqa: E402

EXAMPLE_REPO = BACKEND / "examples" / "vulnerable-service" / "repo"


# =============================================================================
# 固定装置：起一个真的 HTTP 服务
# =============================================================================


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    runs_dir = tmp_path_factory.mktemp("runs")
    original_dir = server.REGISTRY.runs_dir
    server.REGISTRY.runs_dir = runs_dir

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.REGISTRY.runs_dir = original_dir


def get(base: str, path: str) -> tuple[int, str]:
    """不带代理地发请求。带着 HTTP_PROXY 会把 127.0.0.1 也转发出去。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + path, timeout=20) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def post(base: str, path: str, payload: dict) -> tuple[int, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# =============================================================================
# 仓库识别
# =============================================================================


def test_looks_like_repo_finds_src_layout(tmp_path: Path) -> None:
    """源码放在子目录（src/、app/）是常规布局，必须能识别。

    早期版本只在顶层 glob("*.py")，于是 examples/<样本>/repo
    这种 app/ + tests/ 结构的仓库被整个漏掉，列表返回空。
    """
    repo = tmp_path / "svc"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "main.py").write_text("print(1)\n", encoding="utf-8")
    assert server._looks_like_repo(repo) is True


def test_looks_like_repo_accepts_project_marker(tmp_path: Path) -> None:
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert server._looks_like_repo(repo) is True


def test_looks_like_repo_rejects_empty_and_denied(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert server._looks_like_repo(empty) is False

    denied = tmp_path / "__pycache__"
    denied.mkdir()
    (denied / "x.py").write_text("", encoding="utf-8")
    assert server._looks_like_repo(denied) is False


def test_list_repos_returns_example_repo() -> None:
    repos = server.list_repos()
    ids = [item["id"] for item in repos]
    assert "vulnerable-service" in ids
    entry = next(item for item in repos if item["id"] == "vulnerable-service")
    # 6 个源文件；.ruff_cache 之类必须已被排除
    assert entry["files"] == 6


# =============================================================================
# 配置：绝不明文回传密钥
# =============================================================================


def test_config_payload_masks_api_key() -> None:
    key = "sk-" + "0123456789abcdef" * 2
    payload = server.config_payload(Config(api_key=key, base_url="https://api.deepseek.com"))
    assert key not in json.dumps(payload), "配置响应里绝不能出现完整密钥"
    assert payload["api_key_set"] is True
    assert payload["api_key_masked"].startswith("sk-012")
    assert payload["api_key_masked"].endswith("cdef")
    assert payload["provider"] == "DeepSeek"


def test_config_payload_without_key() -> None:
    payload = server.config_payload(Config(api_key=""))
    assert payload["api_key_set"] is False
    assert payload["api_key_masked"] == "(未设置)"
    assert payload["ready"] is False


# =============================================================================
# 工具副标题
# =============================================================================


class _Call:
    def __init__(self, arguments):
        self.arguments = arguments
        self.name = "x"


def test_tool_arg_prefers_path_over_json_dump() -> None:
    assert server._tool_arg(_Call({"path": "app/auth.py", "start": 1})) == "app/auth.py"
    assert server._tool_arg(_Call({"argv": ["pytest", "-q"]})) == "pytest -q"
    assert server._tool_arg(_Call({"todos": [{"text": "a"}]})).startswith('{"todos"')
    assert server._tool_arg(_Call({})) == ""


# =============================================================================
# 写盘格式化
# =============================================================================


def test_format_env_value_drops_trailing_zero() -> None:
    """.env 是给人看也要给人改的，别写 120.0。"""
    assert server.format_env_value(120.0) == "120"
    assert server.format_env_value(0.2) == "0.2"
    assert server.format_env_value(3) == "3"
    assert server.format_env_value("deepseek-flash") == "deepseek-flash"
    assert server.format_env_value(0.0) == "0"


# =============================================================================
# HTTP 集成
# =============================================================================


def test_health_endpoint(live_server: str) -> None:
    status, body = get(live_server, "/api/health")
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["service"] == "margin-harness"


def test_config_endpoint_does_not_leak_key(live_server: str) -> None:
    status, body = get(live_server, "/api/config")
    assert status == 200
    payload = json.loads(body)
    # 只断言「没有明文密钥」，不去比对具体值 —— 测试不该依赖本机 .env 的内容
    assert "api_key" not in payload
    assert "api_key_masked" in payload


def test_static_serves_frontend(live_server: str) -> None:
    status, body = get(live_server, "/")
    assert status == 200
    assert "Margin" in body


@pytest.mark.parametrize(
    "path",
    [
        "/backend/.env",                    # 密钥文件
        "/backend/.env.example",            # 后缀被白名单挡住
        "/backend/harness/server.py",       # 后端源码不应通过静态路由暴露
        "/.git/config",
        "/backend/.runs/whatever.json",
    ],
)
def test_static_denies_sensitive_paths(live_server: str, path: str) -> None:
    status, _ = get(live_server, path)
    assert status == 403, f"{path} 应该被拒绝，实际 {status}"


def test_static_path_traversal_is_blocked(live_server: str) -> None:
    status, _ = get(live_server, "/../../../../etc/passwd")
    assert status in (400, 403, 404)


def test_audit_rejects_missing_repo(live_server: str) -> None:
    status, body = post(live_server, "/api/audit", {"repo": "does/not/exist"})
    assert status == 400
    assert "不存在" in body


def test_audit_accepts_project_relative_path(live_server: str) -> None:
    """相对路径按项目根解析 —— 服务的 CWD 是实现细节，不该出现在接口契约里。"""
    status, body = post(
        live_server,
        "/api/audit",
        {
            "repo": "backend/examples/vulnerable-service/repo",
            "goal": "回归测试：跑通离线链路",
            "offline": True,
        },
    )
    assert status == 201, body
    run_id = json.loads(body)["run_id"]

    deadline = time.time() + 60
    snapshot = {}
    while time.time() < deadline:
        _, raw = get(live_server, f"/api/audit/{run_id}")
        snapshot = json.loads(raw)
        if snapshot.get("status") in ("finished", "error", "stopped"):
            break
        time.sleep(0.2)

    assert snapshot["status"] == "finished", snapshot
    result = snapshot.get("result") or {}
    assert result["stop_reason"] == "finished"
    assert result["counts"]["blocker"] > 0, "离线规则基线应当报出阻断项"
    assert len(result["findings"]) > 0
    assert result.get("report_url")


def test_report_endpoint_renders_markdown(live_server: str) -> None:
    status, body = post(
        live_server,
        "/api/audit",
        {"repo": "backend/examples/vulnerable-service/repo", "offline": True},
    )
    assert status == 201
    run_id = json.loads(body)["run_id"]

    deadline = time.time() + 60
    while time.time() < deadline:
        _, raw = get(live_server, f"/api/audit/{run_id}")
        if json.loads(raw).get("status") == "finished":
            break
        time.sleep(0.2)

    status, body = get(live_server, f"/api/audit/{run_id}/report")
    assert status == 200
    assert "代码审计报告" in body


def test_unknown_audit_id_returns_404(live_server: str) -> None:
    status, _ = get(live_server, "/api/audit/nope-nope")
    assert status == 404


def test_save_config_updates_env_in_place_and_appends(
    live_server: str, tmp_path: Path, monkeypatch
) -> None:
    """保存配置：已有的键原地更新、新键追加，注释与密钥都不能被动。

    刻意把 ENV_PATH 指到临时文件 —— 这个接口默认会写真实的 backend/.env，
    测试绝不能碰用户的真实配置。
    """
    env = tmp_path / ".env"
    env.write_text(
        "# 顶部注释\n"
        "DEEPSEEK_API_KEY=sk-test-keep-me-intact\n"
        "DEEPSEEK_MODEL=deepseek-flash\n"
        "AUDIT_MAX_ITERATIONS=40\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "ENV_PATH", env)

    status, body = post(
        live_server,
        "/api/config",
        {"DEEPSEEK_MODEL": "deepseek-v4-pro", "AUDIT_TEMPERATURE": 0.7, "AUDIT_TIMEOUT": 120},
    )
    assert status == 200, body
    assert set(json.loads(body)["updated"]) == {
        "DEEPSEEK_MODEL",
        "AUDIT_TEMPERATURE",
        "AUDIT_TIMEOUT",
    }

    text = env.read_text(encoding="utf-8")
    assert "# 顶部注释" in text, "注释必须保留"
    assert "DEEPSEEK_API_KEY=sk-test-keep-me-intact" in text, "密钥不能被覆盖"
    assert "DEEPSEEK_MODEL=deepseek-v4-pro" in text, "已有键应原地更新"
    assert "AUDIT_MAX_ITERATIONS=40" in text, "未涉及的键不能丢"
    assert "AUDIT_TEMPERATURE=0.7" in text, "新键应追加"
    assert "AUDIT_TIMEOUT=120\n" in text, "整数值不该写成 120.0"


def test_save_config_rejects_bad_api_key(
    live_server: str, tmp_path: Path, monkeypatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=sk-original\n", encoding="utf-8")
    monkeypatch.setattr(server, "ENV_PATH", env)

    status, body = post(live_server, "/api/config", {"DEEPSEEK_API_KEY": "not-a-key"})
    assert status == 400
    assert "sk-" in body
    assert env.read_text(encoding="utf-8") == "DEEPSEEK_API_KEY=sk-original\n", "拒绝时不应写盘"


def test_save_config_ignores_empty_api_key(
    live_server: str, tmp_path: Path, monkeypatch
) -> None:
    """留空表示「不改密钥」—— 前端就是这么用的，不能把密钥清成空。"""
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=sk-keep\nDEEPSEEK_MODEL=deepseek-flash\n", encoding="utf-8")
    monkeypatch.setattr(server, "ENV_PATH", env)

    status, body = post(live_server, "/api/config", {"DEEPSEEK_API_KEY": "", "DEEPSEEK_MODEL": "deepseek-v4-pro"})
    assert status == 200
    assert "DEEPSEEK_API_KEY" not in json.loads(body)["updated"]
    assert "DEEPSEEK_API_KEY=sk-keep" in env.read_text(encoding="utf-8")


def test_stop_is_idempotent_on_finished_run(live_server: str) -> None:
    status, body = post(live_server, "/api/audit", {"repo": "backend/examples/vulnerable-service/repo", "offline": True})
    run_id = json.loads(body)["run_id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        _, raw = get(live_server, f"/api/audit/{run_id}")
        if json.loads(raw).get("status") in ("finished", "error", "stopped"):
            break
        time.sleep(0.2)
    status, body = post(live_server, f"/api/audit/{run_id}/stop", {})
    assert status == 200
    assert json.loads(body)["stop_requested"] is True


# ===========================================================================
# 工作区：加入 / 移出 / 文件清单 / 审查范围
#
# 这组测试守两件事：
# ① 「加入工作区」不是一个纯 UI 动作 —— 它真的会扩大 Agent 能读的范围，
#    所以相关的密钥护栏必须有回归测试；
# ② 范围（scope）由前端提交，服务端必须自己校验，不能让前端决定边界。
# ===========================================================================


def test_secret_files_are_blocked_from_agent_reads(tmp_path: Path) -> None:
    """把含 .env 的目录加为工作区后，Agent 也不能读到它。

    这是「允许加入任意目录」带来的真实风险：报告与轨迹会流出工作区，
    一次 read_file 就可能把真实密钥写进产物里。HTTP 静态路由只挡浏览器，
    挡不住 Agent 自己，所以护栏必须在工具层。
    """
    from harness.sandbox import SecretFileBlocked, Workspace

    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-real-secret\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    workspace = Workspace(tmp_path)
    with pytest.raises(SecretFileBlocked):
        workspace.read_text(".env")

    # 模板文件是刻意豁免的：只有占位符，且是排查配置的有用线索
    (tmp_path / ".env.example").write_text("DEEPSEEK_API_KEY=sk-xxxx\n", encoding="utf-8")
    text, _ = workspace.read_text(".env.example")
    assert "sk-xxxx" in text

    # 清单里不该出现 .env（列出来但读不了只会让 Agent 白跑几轮）
    listed = [workspace.relative(p) for p in workspace.iter_files()[0]]
    assert ".env" not in listed
    assert "app.py" in listed


def test_iter_files_skips_dot_dirs_but_not_dot_files(tmp_path: Path) -> None:
    """点目录跳过，点文件保留 —— 且工作区根自身含点目录时不能整棵判掉。"""
    from harness.sandbox import Workspace

    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret_state.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("x\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    nested = tmp_path / ".workbuddy" / "proj"
    nested.mkdir(parents=True)
    (nested / "app.py").write_text("y = 2\n", encoding="utf-8")

    listed = [Workspace(tmp_path).relative(p) for p in Workspace(tmp_path).iter_files()[0]]
    assert "app.py" in listed
    assert ".gitignore" in listed, "点文件属于仓库的一部分，不该被跳过"
    assert not any(".hidden" in item for item in listed), "点目录应跳过"

    # 根路径自身位于点目录下：不能因为 parts 里含 .workbuddy 就把整棵树判掉
    inner = [Workspace(nested).relative(p) for p in Workspace(nested).iter_files()[0]]
    assert inner == ["app.py"]


def test_workspace_add_and_remove_round_trip(tmp_path: Path) -> None:
    workspace = tmp_path / "another-repo"
    (workspace / "app").mkdir(parents=True)
    (workspace / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")

    entry, error = server.add_repo(str(workspace))
    assert error == ""
    assert entry is not None
    assert entry["source"] == "added"
    assert entry["removable"] is True
    assert entry["files"] == 1

    # 重复加入不应产生两条记录
    server.add_repo(str(workspace))
    matched = [r for r in server.list_repos() if r["path"] == str(workspace.resolve())]
    assert len(matched) == 1

    ok, error = server.remove_repo(str(workspace))
    assert ok is True and error == ""
    assert all(r["path"] != str(workspace.resolve()) for r in server.list_repos())


def test_workspace_add_rejects_bad_paths(tmp_path: Path) -> None:
    # 空路径
    entry, error = server.add_repo("")
    assert entry is None and "不能为空" in error

    # 不存在的目录
    entry, error = server.add_repo(str(tmp_path / "nope"))
    assert entry is None and "不是" in error

    # 文件而不是目录
    blob = tmp_path / "file.py"
    blob.write_text("x = 1\n", encoding="utf-8")
    entry, error = server.add_repo(str(blob))
    assert entry is None

    # 空目录（没有任何可审文件）
    empty = tmp_path / "empty"
    empty.mkdir()
    entry, error = server.add_repo(str(empty))
    assert entry is None and "没有任何可审的文件" in error


def test_builtin_samples_cannot_be_removed() -> None:
    """examples/ 下的样本是内置的，不该被「移出工作区」误删。"""
    sample = server.BACKEND_ROOT / "examples" / "s3-input-paths" / "repo"
    ok, error = server.remove_repo(str(sample))
    assert ok is False
    assert "不能移除" in error


def test_repo_file_list_matches_repo_file_count() -> None:
    """候选列表的文件数必须与「选择审查范围」列出的条数一致。

    这两处曾经各写一套过滤规则，同一个仓库一个显示 61、一个列出 71，
    用户没法信任任何一边。
    """
    for repo in server.list_repos():
        files, error = server.list_repo_files(repo["path"], limit=5000)
        assert error == ""
        assert len(files) == repo["files"], (
            f"{repo['id']} 的文件数不一致：候选 {repo['files']} vs 清单 {len(files)}"
        )


def test_audit_records_scope_and_rejects_escaping_paths(live_server: str) -> None:
    """scope 会写进运行记录；越界路径被服务端丢弃，不交给前端决定边界。"""
    status, body = post(
        live_server,
        "/api/audit",
        {
            "repo": "backend/examples/s3-input-paths/repo",
            "goal": "只看 app/files.py",
            "offline": True,
            "scope": ["app/files.py", "../../../etc/passwd"],
        },
    )
    assert status == 201
    run_id = json.loads(body)["run_id"]

    deadline = time.time() + 90
    record = None
    while time.time() < deadline:
        _, raw = get(live_server, f"/api/audit/{run_id}")
        record = json.loads(raw)
        if record.get("status") in ("finished", "error", "stopped"):
            break
        time.sleep(0.2)

    assert record is not None
    assert record["scope"] == ["app/files.py", "../../../etc/passwd"], "原始 scope 应原样留档"

    # 越界项必须被丢掉，且不能把仓库外的路径原文喂给模型
    note = record["context_note"]
    assert "app/files.py" in note
    assert "passwd" not in note, "越界路径的原文不应出现在模型上下文里"
    assert "1 个路径超出仓库范围" in note, "只报个数，不回显路径"


# =============================================================================
# 可插拔 Agent（HTTP 层）
#
# 这一层要挡住的是「用户选了一个不存在的助手，却拿到一份跑完了的结果」——
# 那比直接报错难发现得多。同时要确认产出不是只有 findings 一种形状。
# =============================================================================


def test_agents_endpoint_lists_manifests(live_server: str) -> None:
    status, body = get(live_server, "/api/agents")
    assert status == 200
    payload = json.loads(body)
    ids = [item["id"] for item in payload["agents"]]
    assert "review" in ids and "architect" in ids
    assert payload["default"] == "review"

    # 前端的助手轨道完全靠这些字段渲染，缺一个就得改前端
    architect = next(item for item in payload["agents"] if item["id"] == "architect")
    assert architect["outcome"] == "blueprint"
    assert architect["artifact_kinds"] == ["layer", "component", "decision", "open_question"]
    assert architect["accent"].startswith("#")
    assert architect["can_edit"] is False


def test_audit_rejects_unknown_agent(live_server: str) -> None:
    status, body = post(
        live_server,
        "/api/audit",
        {"repo": "backend/examples/s3-input-paths/repo", "goal": "x", "offline": True, "agent": "nope"},
    )
    assert status == 400
    payload = json.loads(body)
    assert payload.get("code") == "unknown_agent"
    assert "review" in payload["error"], "错误信息要告诉调用方有哪些可用"


def test_audit_runs_architect_and_returns_blueprint_artifacts(live_server: str) -> None:
    status, body = post(
        live_server,
        "/api/audit",
        {
            "repo": "backend/examples/s3-input-paths/repo",
            "goal": "盘一下结构",
            "offline": True,
            "agent": "architect",
        },
    )
    assert status == 201
    started = json.loads(body)
    assert started["agent"] == "architect"
    assert started["outcome"] == "blueprint"

    run_id = started["run_id"]
    deadline = time.time() + 90
    record = None
    while time.time() < deadline:
        _, raw = get(live_server, f"/api/audit/{run_id}")
        record = json.loads(raw)
        if record.get("status") in ("finished", "error", "stopped"):
            break
        time.sleep(0.2)

    assert record is not None
    assert record["status"] == "finished", record.get("error")
    assert record["agent"] == "architect", "历史回放也要知道这次用的是哪个 agent"

    result = record["result"]
    assert result["outcome"] == "blueprint"
    artifacts = result["artifacts"]
    assert artifacts["component"], "架构运行应当产出组件"
    assert artifacts["layer"], "架构运行应当产出分层"
    # 架构助手不产出发现，findings 保持为空
    assert result["findings"] == []
    # counts 仍是严重级别语义（前端按 counts.blocker 渲染），通用计数另看 artifact_counts
    assert result["counts"]["blocker"] == 0
    assert result["artifact_counts"]["component"] >= 1


def test_audit_defaults_to_review_agent(live_server: str) -> None:
    """不传 agent 时行为必须与改造前一致。"""
    status, body = post(
        live_server,
        "/api/audit",
        {"repo": "backend/examples/vulnerable-service/repo", "goal": "找缺陷", "offline": True},
    )
    assert status == 201
    started = json.loads(body)
    assert started["agent"] == "review"
    assert started["outcome"] == "findings"

    run_id = started["run_id"]
    deadline = time.time() + 90
    record = None
    while time.time() < deadline:
        _, raw = get(live_server, f"/api/audit/{run_id}")
        record = json.loads(raw)
        if record.get("status") in ("finished", "error", "stopped"):
            break
        time.sleep(0.2)

    assert record is not None
    assert record["status"] == "finished", record.get("error")
    assert record["result"]["findings"], "审查运行应当产出发现"
    assert record["result"]["artifacts"]["finding"]


def test_report_replays_every_artifact_bucket(live_server: str) -> None:
    """报告是从终态 JSON 重放的，重放必须还原**全部**产出桶。

    只还原 finding 会让架构报告渲染出「本次没有登记分层」——
    那不是事实，只是回放路径丢了一半产出。
    """
    status, body = post(
        live_server,
        "/api/audit",
        {"repo": "backend/examples/s3-input-paths/repo", "goal": "盘一下", "offline": True, "agent": "architect"},
    )
    assert status == 201
    run_id = json.loads(body)["run_id"]

    deadline = time.time() + 90
    record = None
    while time.time() < deadline:
        _, raw = get(live_server, f"/api/audit/{run_id}")
        record = json.loads(raw)
        if record.get("status") in ("finished", "error", "stopped"):
            break
        time.sleep(0.2)
    assert record is not None and record["status"] == "finished"

    status, text = get(live_server, f"/api/audit/{run_id}/report")
    assert status == 200
    assert "# 架构设计报告" in text
    assert "本次没有登记分层" not in text, "重放丢了分层产出"
    assert "本次没有登记组件" not in text, "重放丢了组件产出"
    assert "开放问题" in text


def _wait_run(base: str, run_id: str) -> dict:
    deadline = time.time() + 90
    while time.time() < deadline:
        _, raw = get(base, f"/api/audit/{run_id}")
        record = json.loads(raw)
        if record.get("status") in ("finished", "error", "stopped"):
            return record
        time.sleep(0.2)
    raise AssertionError("运行超时未结束")


def test_runs_and_repos_can_be_scoped_to_one_agent(live_server: str) -> None:
    """助手之间的历史必须隔离：列表与仓库计数都得跟着过滤。

    这两处如果不同口径（列表按助手过滤、计数算全部），用户会看到
    「这个助手名下 5 次」但一条都点不出来 —— 信任问题。
    """
    repo = "backend/examples/s1-obvious-danger/repo"
    status, body = post(live_server, "/api/audit", {"repo": repo, "goal": "审一下", "offline": True, "agent": "review"})
    assert status == 201
    review_run = json.loads(body)["run_id"]
    _wait_run(live_server, review_run)

    status, body = post(live_server, "/api/audit", {"repo": repo, "goal": "盘一下", "offline": True, "agent": "architect"})
    assert status == 201
    arch_run = json.loads(body)["run_id"]
    _wait_run(live_server, arch_run)

    _, raw = get(live_server, "/api/runs")
    every = {r["run_id"]: r["agent"] for r in json.loads(raw)["runs"]}
    assert every[review_run] == "review"
    assert every[arch_run] == "architect"

    _, raw = get(live_server, "/api/runs?agent=architect")
    archs = [r["run_id"] for r in json.loads(raw)["runs"]]
    assert archs, "架构助手名下应当有自己的运行"
    assert review_run not in archs, "审查运行混进了架构助手的历史里"
    assert all(every[r] == "architect" for r in archs), "过滤后混入了别的助手"


def test_unknown_agent_is_rejected_on_every_list_endpoint(live_server: str) -> None:
    """/api/audit 已经拒绝未知助手；列表接口也必须同样严格。

    静默返回「空列表」会让前端以为这个助手没有历史。
    """
    for path in ("/api/runs?agent=nope", "/api/repos?agent=nope"):
        status, body = get(live_server, path)
        assert status == 400, path
        payload = json.loads(body)
        assert payload["code"] == "unknown_agent", path
        assert payload["agents"], "错误里要带上可用清单，方便调用方纠正"


def test_repo_run_count_matches_filtered_session_count(live_server: str) -> None:
    """仓库卡上的「N 次」必须等于这一栏里能点出来的条数。"""
    repo = "backend/examples/s2-resources-errors/repo"
    status, body = post(live_server, "/api/audit", {"repo": repo, "goal": "盘一下", "offline": True, "agent": "architect"})
    assert status == 201
    _wait_run(live_server, json.loads(body)["run_id"])

    _, raw = get(live_server, "/api/repos?agent=architect")
    repos = json.loads(raw)["repos"]
    entry = next(r for r in repos if r["path"].endswith("s2-resources-errors\\repo") or r["path"].endswith("s2-resources-errors/repo"))

    _, raw = get(live_server, "/api/runs?agent=architect")
    listed = [r for r in json.loads(raw)["runs"] if r["repo"] == entry["path"]]

    assert entry["runs"] == len(listed), "仓库计数与会话列表不同口径"


def test_archived_workspace_file_is_not_a_run(tmp_path: Path) -> None:
    """.runs/ 目录里混着工作区清单 workspace.json，它不能被当成一条运行。

    读进来会变成一条 id=workspace、目标为空的记录，侧栏里就多出
    一个谁也说不清的「未命名任务」。按形状筛，不按文件名筛。
    """
    from harness.server import RunRegistry

    (tmp_path / "workspace.json").write_text('{"roots": []}', encoding="utf-8")
    (tmp_path / "abc123.json").write_text(
        json.dumps({"run_id": "abc123", "created_at": 1.0, "repo": "r", "status": "finished"}),
        encoding="utf-8",
    )

    registry = RunRegistry(runs_dir=tmp_path)
    assert registry.load_archived() == 1, "只应加载真正是运行的那个文件"
    ids = [r["run_id"] for r in registry.list()]
    assert ids == ["abc123"], ids
