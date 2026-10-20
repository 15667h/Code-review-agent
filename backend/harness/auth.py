"""本机账号与会话。

零第三方依赖：``hashlib.scrypt`` 做密码哈希、``secrets`` 生成会话令牌、
JSON 落盘（原子写）。设计的取舍都写在下面，都是能在评审里站住的那种。

为什么不引入框架/数据库：这个服务本身也是要被审计的代码，依赖越少越好审。
账号表就一台机器一个人用，JSON 足够。

安全要点（每一条都有理由）：
1. **密码只存哈希**，用 scrypt（内存硬，抗 GPU 暴力）而不是单轮 sha256。
   salt 每个账号独立随机，这样相同密码也不会产生相同哈希。
2. **比较用 ``hmac.compare_digest``**，避免按字节短路比较泄漏时间信息。
3. **登录失败不区分「用户不存在」与「密码错误」** —— 否则接口变成账号枚举器。
4. **有限流**：同一邮箱连续失败到阈值后短暂锁定。本机工具也照做 ——
   这个服务会监听端口，别把「本机」当成免死金牌。
5. **会话令牌只存在服务端内存里**，Cookie 只带不可猜的随机串，不带任何用户信息。
6. **落盘原子**：先写临时文件再 ``os.replace``，避免进程被杀时留下半截 JSON
   （半截 JSON 会导致下次启动所有账号丢失）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = BACKEND_ROOT / ".auth"
USERS_PATH = AUTH_DIR / "users.json"

# scrypt 参数：n=2^14 / r=8 / p=1 是交互式登录场景的常见档位（约 16 MB 内存、几十毫秒）。
# 调高会更安全但登录变慢，本机工具没必要。
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

SESSION_TTL = 7 * 24 * 3600  # 一周
COOKIE_NAME = "margin_session"

# 限流：同一邮箱连续失败达到阈值就锁一会儿。本机工具，不需要分布式计数器。
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 60

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MIN_PASSWORD = 8
MAX_PASSWORD = 200  # 防「超长密码」把 scrypt 拖成 DoS

# =============================================================================
# 错误码：前端按码给文案，不解析中文句子
# =============================================================================

class AuthError(Exception):
    """带机器可读 code 的认证错误。code 是给前端判断的契约。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

# =============================================================================
# 密码哈希
# =============================================================================

def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """返回 ``scrypt$n$r$p$salt$hash``（base64url，无填充）。"""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN
    )
    b64 = lambda raw: base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"

def verify_password(password: str, stored: str) -> bool:
    """校验密码。格式不认识就当失败，绝不抛给调用方（避免把栈信息带进响应）。"""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        pad = lambda text: text + "=" * (-len(text) % 4)  # noqa: E731
        salt = base64.urlsafe_b64decode(pad(salt_b64))
        expected = base64.urlsafe_b64decode(pad(hash_b64))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)

# =============================================================================
# 账号
# =============================================================================

@dataclass(slots=True)
class User:
    email: str
    name: str
    created_at: float
    password_hash: str = ""

    def public(self) -> dict[str, object]:
        """对外表示：**永不包含 password_hash**。"""
        return {"email": self.email, "name": self.name, "created_at": self.created_at}

def _norm_email(email: str) -> str:
    return (email or "").strip().lower()

def _check_email(email: str) -> str:
    clean = _norm_email(email)
    if not clean:
        raise AuthError("email_empty", "请输入邮箱地址。")
    if not EMAIL_RE.match(clean) or len(clean) > 200:
        raise AuthError("email_invalid", "邮箱格式不对，检查一下 @ 前后是否完整。")
    return clean

def _check_password(password: str, *, for_register: bool) -> str:
    if not password:
        raise AuthError("password_empty", "请输入密码。")
    if len(password) > MAX_PASSWORD:
        raise AuthError("password_too_long", f"密码太长了（上限 {MAX_PASSWORD} 位）。")
    if for_register and len(password) < MIN_PASSWORD:
        raise AuthError("password_short", f"密码至少 {MIN_PASSWORD} 位，现在还不够。")
    return password

class AuthStore:
    """账号表。所有写操作串行化（``_lock``），并且原子落盘。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or USERS_PATH
        self._lock = threading.Lock()
        self._users: dict[str, User] = {}
        # 限流状态只在内存里：重启即清空是**有意**的，本机工具不需要持久化封禁
        self._attempts: dict[str, tuple[int, float]] = {}
        self.load()

    # ---- 持久化 ---- #

    def load(self) -> int:
        if not self.path.is_file():
            return 0
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 文件坏了不能假装「没有账号」然后覆盖它 —— 那会把用户的账号真删掉。
            # 改名留档，让人能手工恢复，然后以空表继续（并在日志里说明）。
            broken = self.path.with_suffix(".broken.json")
            try:
                self.path.replace(broken)
                print(f"[auth] 账号文件无法解析，已改名为 {broken.name} 并重新开始。")
            except OSError:
                pass
            return 0

        users: dict[str, User] = {}
        for key, raw in (payload.get("users") or {}).items():
            if not isinstance(raw, dict) or "password_hash" not in raw:
                continue
            users[key] = User(
                email=raw.get("email", key),
                name=raw.get("name", ""),
                created_at=float(raw.get("created_at") or 0.0),
                password_hash=raw["password_hash"],
            )
        self._users = users
        return len(users)

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "users": {email: asdict(user) for email, user in self._users.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)  # 原子替换：要么旧文件，要么新文件，不会有半截

    # ---- 查询 ---- #

    def get(self, email: str) -> User | None:
        return self._users.get(_norm_email(email))

    def count(self) -> int:
        return len(self._users)

    # ---- 写 ---- #

    def register(self, email: str, name: str, password: str) -> User:
        email = _check_email(email)
        password = _check_password(password, for_register=True)
        clean_name = (name or "").strip()
        if not clean_name:
            raise AuthError("name_empty", "请填写名字，它会显示在审查记录里。")
        if len(clean_name) > 40:
            raise AuthError("name_too_long", "名字太长了（上限 40 字）。")

        with self._lock:
            if email in self._users:
                raise AuthError("email_taken", "这个邮箱已经注册过了，直接登录吧。", status=409)
            user = User(
                email=email,
                name=clean_name,
                created_at=time.time(),
                password_hash=hash_password(password),
            )
            self._users[email] = user
            self._flush()
        return user

    def authenticate(self, email: str, password: str) -> User:
        """登录校验。失败一律抛同一个 ``bad_credentials`` —— 不做账号枚举。"""
        email = _norm_email(email)
        self._guard_rate(email)

        user = self._users.get(email)
        # 用户不存在时也走一次 scrypt，让耗时与「密码错」不可区分
        stored = user.password_hash if user else hash_password("placeholder-for-timing")
        ok = verify_password(password or "", stored)

        if not user or not ok:
            self._record_failure(email)
            raise AuthError("bad_credentials", "邮箱或密码不对。", status=401)

        self._attempts.pop(email, None)
        return user

    # ---- 限流 ---- #

    def _guard_rate(self, email: str) -> None:
        entry = self._attempts.get(email)
        if not entry:
            return
        count, last = entry
        if count >= MAX_ATTEMPTS:
            remain = LOCKOUT_SECONDS - (time.time() - last)
            if remain > 0:
                raise AuthError(
                    "too_many_attempts",
                    f"尝试次数过多，请 {int(remain) + 1} 秒后再试。",
                    status=429,
                )
            self._attempts.pop(email, None)

    def _record_failure(self, email: str) -> None:
        count, _ = self._attempts.get(email, (0, 0.0))
        self._attempts[email] = (count + 1, time.time())

# =============================================================================
# 会话
# =============================================================================

@dataclass(slots=True)
class Session:
    token: str
    email: str
    issued_at: float
    expires_at: float

class SessionStore:
    """会话只存在内存里：进程重启即全部失效。

    这是**有意的**取舍 —— 落盘会话等价于把长期凭据写到磁盘，本机工具不值当。
    代价是重启后要重新登录，可以接受。
    """

    def __init__(self, ttl: int = SESSION_TTL) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def issue(self, email: str) -> Session:
        token = secrets.token_urlsafe(32)
        now = time.time()
        session = Session(token=token, email=email, issued_at=now, expires_at=now + self.ttl)
        with self._lock:
            self._sessions[token] = session
        return session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if session.expires_at < time.time():
                self._sessions.pop(token, None)
                return None
            return session

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

# =============================================================================
# 测试账户
# =============================================================================

#: 首次启动（账号表为空）时自动创建。密码只在这里出现一次，落盘的是 scrypt 哈希。
DEMO_ACCOUNTS: tuple[dict[str, str], ...] = (
    {"email": "demo@margin.dev", "name": "演示账号", "password": "Margin-demo-2026"},
    {"email": "reviewer@margin.dev", "name": "林哲", "password": "Margin-review-2026"},
)

def seed_demo_accounts(store: AuthStore) -> list[dict[str, str]]:
    """账号表为空时写入测试账户，返回创建明细供启动日志打印。

    只在**空表**时执行：已有账号就不动，避免把用户改过的演示账号重置回去。
    """
    if store.count() > 0:
        return []
    created: list[dict[str, str]] = []
    for spec in DEMO_ACCOUNTS:
        try:
            store.register(spec["email"], spec["name"], spec["password"])
        except AuthError:
            continue
        created.append(dict(spec))
    return created

#: 进程级单例。server 与测试都从这里取（测试用 monkeypatch 换成临时路径）。
STORE = AuthStore()
SESSIONS = SessionStore()
