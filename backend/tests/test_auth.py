"""账号与会话的回归测试。

认证是安全敏感面：这里的每一条断言都对应一个「做错了会真出事」的点
（密码落盘明文、账号枚举、会话不吊销、限流失效、开关打开后仍能匿名访问）。
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from harness import auth, server  # noqa: E402

@pytest.fixture
def store(tmp_path: Path) -> auth.AuthStore:
    return auth.AuthStore(path=tmp_path / "users.json")

# =============================================================================
# 密码哈希
# =============================================================================

def test_password_is_never_stored_in_clear(store: auth.AuthStore) -> None:
    """落盘的东西里不能出现明文密码 —— 这是这一层的底线。"""
    user = store.register("a@b.dev", "甲", "S3cret-pass")
    raw = (store.path).read_text(encoding="utf-8")

    assert "S3cret-pass" not in raw, "密码明文进了存储文件"
    assert user.password_hash.startswith("scrypt$")
    assert "S3cret-pass" not in json.dumps(user.public()), "public() 泄漏了哈希/密码"
    assert "password_hash" not in user.public()

def test_same_password_gets_different_hashes(store: auth.AuthStore) -> None:
    """salt 要随机：两个用户用同一个密码，哈希也必须不同。"""
    store.register("a@b.dev", "甲", "same-password-1")
    store.register("c@d.dev", "乙", "same-password-1")
    first = store.get("a@b.dev").password_hash
    second = store.get("c@d.dev").password_hash
    assert first != second

def test_verify_password_accepts_correct_and_rejects_everything_else() -> None:
    stored = auth.hash_password("correct-horse")
    assert auth.verify_password("correct-horse", stored) is True
    assert auth.verify_password("wrong", stored) is False
    # 格式损坏时返回 False，而不是抛异常把栈带到响应里
    for broken in ("", "not-a-hash", "scrypt$bad", "bcrypt$1$2$3$4$5"):
        assert auth.verify_password("correct-horse", broken) is False

# =============================================================================
# 注册
# =============================================================================

def test_register_validates_every_field(store: auth.AuthStore) -> None:
    cases = [
        (("", "甲", "longenough1"), "email_empty"),
        (("nope", "甲", "longenough1"), "email_invalid"),
        (("a@b.dev", "", "longenough1"), "name_empty"),
        (("a@b.dev", "甲", ""), "password_empty"),
        (("a@b.dev", "甲", "short"), "password_short"),
        (("a@b.dev", "甲", "x" * 300), "password_too_long"),
    ]
    for args, expected in cases:
        with pytest.raises(auth.AuthError) as exc:
            store.register(*args)
        assert exc.value.code == expected, args

def test_register_rejects_duplicate_email(store: auth.AuthStore) -> None:
    store.register("a@b.dev", "甲", "longenough1")
    with pytest.raises(auth.AuthError) as exc:
        store.register("A@B.DEV", "乙", "longenough2")  # 大小写视为同一个邮箱
    assert exc.value.code == "email_taken"
    assert exc.value.status == 409

def test_register_normalises_email(store: auth.AuthStore) -> None:
    user = store.register("  Mixed@Case.DEV ", "甲", "longenough1")
    assert user.email == "mixed@case.dev"
    assert store.get("MIXED@case.dev") is not None

# =============================================================================
# 登录
# =============================================================================

def test_login_does_not_reveal_whether_account_exists(store: auth.AuthStore) -> None:
    """用户不存在与密码错误必须是同一个错误码，否则接口变成账号枚举器。"""
    store.register("a@b.dev", "甲", "longenough1")

    with pytest.raises(auth.AuthError) as wrong_password:
        store.authenticate("a@b.dev", "wrong-password")
    with pytest.raises(auth.AuthError) as no_such_user:
        store.authenticate("ghost@b.dev", "whatever1")

    assert wrong_password.value.code == no_such_user.value.code == "bad_credentials"
    assert wrong_password.value.status == 401

def test_login_succeeds_with_correct_password(store: auth.AuthStore) -> None:
    store.register("a@b.dev", "甲", "longenough1")
    assert store.authenticate("A@B.dev", "longenough1").email == "a@b.dev"

def test_login_locks_out_after_repeated_failures(store: auth.AuthStore) -> None:
    """连续失败要限流：本机工具也不能把「监听端口」当成免死金牌。"""
    store.register("a@b.dev", "甲", "longenough1")
    for _ in range(auth.MAX_ATTEMPTS):
        with pytest.raises(auth.AuthError):
            store.authenticate("a@b.dev", "wrong")

    with pytest.raises(auth.AuthError) as exc:
        store.authenticate("a@b.dev", "longenough1")  # 连正确密码也先挡住
    assert exc.value.code == "too_many_attempts"
    assert exc.value.status == 429

def test_successful_login_resets_failure_counter(store: auth.AuthStore) -> None:
    store.register("a@b.dev", "甲", "longenough1")
    for _ in range(auth.MAX_ATTEMPTS - 1):
        with pytest.raises(auth.AuthError):
            store.authenticate("a@b.dev", "wrong")
    store.authenticate("a@b.dev", "longenough1")
    # 计数已清零，还能再失败若干次才被锁
    for _ in range(auth.MAX_ATTEMPTS - 1):
        with pytest.raises(auth.AuthError) as exc:
            store.authenticate("a@b.dev", "wrong")
        assert exc.value.code == "bad_credentials"

# =============================================================================
# 存储健壮性与会话
# =============================================================================

def test_accounts_survive_a_reload(tmp_path: Path) -> None:
    path = tmp_path / "users.json"
    first = auth.AuthStore(path=path)
    first.register("a@b.dev", "甲", "longenough1")

    second = auth.AuthStore(path=path)
    assert second.count() == 1
    assert second.authenticate("a@b.dev", "longenough1").name == "甲"

def test_broken_file_is_preserved_not_silently_dropped(tmp_path: Path) -> None:
    """文件坏了要留档：直接当空表会让用户的账号无声消失。"""
    path = tmp_path / "users.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")

    store = auth.AuthStore(path=path)
    assert store.count() == 0
    assert (tmp_path / "users.broken.json").is_file(), "坏文件没有被留档"

def test_session_lifecycle() -> None:
    sessions = auth.SessionStore(ttl=60)
    token = sessions.issue("a@b.dev").token

    assert sessions.get(token).email == "a@b.dev"
    assert sessions.get("forged-token") is None
    assert sessions.get(None) is None
    assert sessions.revoke(token) is True
    assert sessions.get(token) is None, "吊销后不能再用"
    assert sessions.revoke(token) is False

def test_expired_session_is_rejected() -> None:
    sessions = auth.SessionStore(ttl=-1)  # 立即过期
    token = sessions.issue("a@b.dev").token
    assert sessions.get(token) is None

def test_seed_only_runs_on_an_empty_store(store: auth.AuthStore) -> None:
    created = auth.seed_demo_accounts(store)
    assert len(created) == len(auth.DEMO_ACCOUNTS)
    for item in created:
        assert store.authenticate(item["email"], item["password"]) is not None, item

    # 已有账号时不能再动 —— 否则会把用户改过的密码重置回演示值
    assert auth.seed_demo_accounts(store) == []

# =============================================================================
# HTTP 集成
# =============================================================================

@pytest.fixture
def auth_server(tmp_path: Path):
    """起一个真服务，账号表指向临时目录。"""
    store = auth.AuthStore(path=tmp_path / "users.json")
    sessions = auth.SessionStore()
    auth.seed_demo_accounts(store)

    original = (auth.STORE, auth.SESSIONS, server.REGISTRY.runs_dir)
    auth.STORE, auth.SESSIONS = store, sessions
    server.REGISTRY.runs_dir = tmp_path / "runs"

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        auth.STORE, auth.SESSIONS, server.REGISTRY.runs_dir = original

def _request(base, path, *, method="GET", body=None, cookie=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), resp.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload, exc.headers

def test_http_login_me_logout_round_trip(auth_server: str) -> None:
    status, payload, headers = _request(
        auth_server, "/api/auth/login", method="POST",
        body={"email": "demo@margin.dev", "password": "Margin-demo-2026"},
    )
    assert status == 200
    assert payload["user"]["email"] == "demo@margin.dev"

    cookie_header = headers.get("Set-Cookie") or ""
    assert auth.COOKIE_NAME in cookie_header
    assert "HttpOnly" in cookie_header, "会话 Cookie 必须 HttpOnly（脚本不可读）"
    assert "SameSite=Strict" in cookie_header, "防跨站请求带上会话"
    token = cookie_header.split(";")[0]

    status, payload, _ = _request(auth_server, "/api/auth/me", cookie=token)
    assert status == 200 and payload["user"]["email"] == "demo@margin.dev"

    status, _, headers = _request(auth_server, "/api/auth/logout", method="POST", body={}, cookie=token)
    assert status == 200
    assert "Max-Age=0" in (headers.get("Set-Cookie") or ""), "登出要清掉 Cookie"

    status, payload, _ = _request(auth_server, "/api/auth/me", cookie=token)
    assert payload["user"] is None, "吊销后的会话不能再认"

def test_http_wrong_password_is_401_without_leaking(auth_server: str) -> None:
    status, payload, _ = _request(
        auth_server, "/api/auth/login", method="POST",
        body={"email": "demo@margin.dev", "password": "definitely-wrong"},
    )
    assert status == 401
    assert payload["code"] == "bad_credentials"
    assert "demo@margin.dev" in payload["error"] or "邮箱或密码" in payload["error"]

def test_http_register_then_use_the_new_account(auth_server: str) -> None:
    status, payload, headers = _request(
        auth_server, "/api/auth/register", method="POST",
        body={"email": "new@margin.dev", "name": "新同学", "password": "brand-new-2026"},
    )
    assert status == 201
    assert payload["user"]["name"] == "新同学"
    token = (headers.get("Set-Cookie") or "").split(";")[0]

    status, payload, _ = _request(auth_server, "/api/auth/me", cookie=token)
    assert payload["user"]["email"] == "new@margin.dev"

    # 重复注册同一个邮箱：409，且不改动已有账号
    status, payload, _ = _request(
        auth_server, "/api/auth/register", method="POST",
        body={"email": "new@margin.dev", "name": "冒名", "password": "another-2026"},
    )
    assert status == 409 and payload["code"] == "email_taken"

def test_anonymous_access_is_allowed_by_default(auth_server: str) -> None:
    """默认开关是关的：本机工具直开即用，登录只用来「知道自己是谁」。"""
    status, payload, _ = _request(auth_server, "/api/auth/me")
    assert status == 200 and payload["user"] is None

    status, _, _ = _request(auth_server, "/api/runs")
    assert status == 200, "默认不该拦业务接口"

def test_require_auth_blocks_business_endpoints(auth_server: str, monkeypatch) -> None:
    """打开 MARGIN_REQUIRE_AUTH 后：业务接口 401，公开接口照常。"""
    from harness.config import Config

    monkeypatch.setattr(server, "load_config", lambda *a, **k: Config(require_auth=True))

    status, payload, _ = _request(auth_server, "/api/runs")
    assert status == 401
    assert payload["code"] == "unauthenticated"
    assert payload["login"] == "/login.html", "响应要告诉前端去哪儿登录"

    status, _, _ = _request(auth_server, "/api/health")
    assert status == 200, "健康检查不能被挡（否则探活会误判服务已死）"

    status, payload, headers = _request(
        auth_server, "/api/auth/login", method="POST",
        body={"email": "demo@margin.dev", "password": "Margin-demo-2026"},
    )
    assert status == 200, "登录接口本身不能要求先登录"
    token = (headers.get("Set-Cookie") or "").split(";")[0]

    status, _, _ = _request(auth_server, "/api/runs", cookie=token)
    assert status == 200, "登录后应能正常访问"

def test_set_cookie_does_not_leak_across_requests(auth_server: str) -> None:
    """keep-alive 下 Handler 实例会复用 —— 排队头必须每请求重置。"""
    _, _, login_headers = _request(
        auth_server, "/api/auth/login", method="POST",
        body={"email": "demo@margin.dev", "password": "Margin-demo-2026"},
    )
    assert login_headers.get("Set-Cookie")

    # 紧接着一个不带 Cookie 的普通请求，不该再收到 Set-Cookie
    status, _, headers = _request(auth_server, "/api/health")
    assert status == 200
    assert not headers.get("Set-Cookie"), "上一个请求的 Set-Cookie 漏到了下一个响应"

def test_login_response_never_includes_the_password_hash(auth_server: str) -> None:
    status, payload, _ = _request(
        auth_server, "/api/auth/login", method="POST",
        body={"email": "reviewer@margin.dev", "password": "Margin-review-2026"},
    )
    assert status == 200
    blob = json.dumps(payload)
    assert "scrypt" not in blob and "password" not in blob

def test_frontend_demo_credentials_match_backend_seed() -> None:
    """前端一键登录用的密码必须与后端 seed 的完全一致。

    同一个密码硬编码在两处（前端常量 + 后端 seed）就有漂移风险，而漂移的
    后果很具体：**用户照着页面上写的密码登录，报「邮箱或密码不对」**。
    这条测试把两处钉在一起 —— 改一处而忘了另一处，测试立刻红。

    （页面上的提示文本也由同一份前端常量渲染，所以提示里写的必然是可用的。）
    """
    import re

    script = (BACKEND.parent / "scripts" / "auth.js").read_text(encoding="utf-8")
    block = script.split("var DEMO_CREDENTIALS = {", 1)[1].split("};", 1)[0]
    frontend = dict(re.findall(r"'([^']+@[^']+)':\s*'([^']+)'", block))

    backend = {item["email"]: item["password"] for item in auth.DEMO_ACCOUNTS}
    assert frontend == backend, (
        "前端 DEMO_CREDENTIALS 与后端 DEMO_ACCOUNTS 不一致：\n"
        f"  前端 {frontend}\n  后端 {backend}"
    )
    assert frontend, "没有解析到前端常量，检查 auth.js 的写法是否变了"

def test_seeded_accounts_are_actually_loginable(store: auth.AuthStore) -> None:
    """seed 出来的账号必须真能登录 —— 「提供测试账户」的意义就在这里。"""
    created = auth.seed_demo_accounts(store)
    assert created, "空表时应创建测试账户"
    for item in auth.DEMO_ACCOUNTS:
        user = store.authenticate(item["email"], item["password"])
        assert user.email == item["email"]
        assert user.name == item["name"]
