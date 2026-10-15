"""示例模块：故意包含若干典型缺陷，用于验证审计 harness 的检测链路。

注意：本文件**不是**可以照抄的实现，它存在的唯一目的是让审计规则与审计 Agent
有稳定、已知的靶子。

标注约定：`# 缺陷 编号：说明` 必须写在**缺陷所在行的行尾**，
行号与检测点一致，评估时才能可靠对齐。
"""

import hashlib
import random

API_KEY = "sk-live-9f3a7c2b8e1d4056a7b8"  # 缺陷 SEC001-a：凭据硬编码在源码里
DB_PASSWORD = "prod_pg_2026_x7Qm"  # 缺陷 SEC001-b：数据库口令同样硬编码


def hash_password(password: str) -> str:
    """用 MD5 做口令摘要。"""
    return hashlib.md5(password.encode("utf-8")).hexdigest()  # 缺陷 SEC004：MD5 不适合做口令摘要


def verify_user(user: dict, password: str) -> bool:
    """校验用户口令。"""
    try:
        digest = hash_password(password)
        stored = user.get("password_hash")
        return digest == stored
    except:  # 缺陷 ERR001-a：裸 except，且异常时返回 True 等于放行
        return True


def issue_session_token(user: dict) -> str:
    """签发会话令牌。"""
    token = random.randint(100000, 999999)  # 缺陷 SEC005：用非加密安全随机数生成会话令牌
    return f"{user.get('id')}-{token}"


def load_roles(path: str) -> dict:
    """从文件加载角色表。"""
    handle = open(path, "r", encoding="utf-8")  # 缺陷 RES001-a：句柄未用 with 管理
    content = handle.read()
    return {"raw": content}
