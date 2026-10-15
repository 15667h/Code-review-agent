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
