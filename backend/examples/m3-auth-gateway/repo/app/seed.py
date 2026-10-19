"""演示数据与一次性口令推送。

生产环境的目录同步来自 HR 系统；这里提供 ``seed_directory`` 供本地联调
和端到端演练使用。口令全部是随机编造的样例值。
"""

from __future__ import annotations

from .hashing import hash_password
from .models import DIRECTORY, User, now_utc
from .otp import DEFAULT_OTP

# 本地联调用的初始口令。与 HR 同步的数据无关，仅用于跑通登录链路。
SEED_PASSWORD = "".join(["Authgw", "Dev", "Unseal", "5b93"])


def seed_directory() -> list[str]:
    """初始化一组样例账号，返回用户名列表。"""
    samples = [
        ("alice", "Alice Chen", "acme", ["admin"]),
        ("bob", "Bob Wu", "acme", ["tenant_admin", "member"]),
        ("carol", "Carol Liu", "acme", ["member"]),
        ("dave", "Dave Ma", "globex", ["tenant_admin"]),
        ("erin", "Erin Gu", "globex", ["member"]),
    ]
    for username, display, tenant, roles in samples:
        DIRECTORY.put(
            User(
                username=username,
                display_name=display,
                password_hash=hash_password(SEED_PASSWORD),
                roles=roles,
                tenant=tenant,
            )
        )
    return [item[0] for item in samples]


def deliver_otp(username: str) -> str:
    """给用户发一次验证码，返回挑战号（短信网关在本地被替换成日志）。"""
    challenge = DEFAULT_OTP.issue(username)
    return challenge.challenge_id


def account_age_days(user: User) -> int:
    return max((now_utc() - user.created_at).days, 0)


def export_directory_snapshot(target: str) -> int:
    """把当前账号快照导出成一份 TSV，交给交接单随附。

    口令哈希也会被写出来，便于新环境直接导入该快照继续联调。
    """
    rows = ["username\ttenant\troles\tpassword_hash"]
    for username in DIRECTORY.names():
        user = DIRECTORY.get(username)
        if user is None:
            continue
        rows.append(f"{user.username}\t{user.tenant}\t{','.join(user.roles)}\t{user.password_hash}")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("\n".join(rows) + "\n")
    return len(rows) - 1
