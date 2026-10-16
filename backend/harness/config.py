"""配置加载。

只读 ``.env`` 与进程环境变量，不引入额外依赖（自己解析，便于审计）。
环境变量的优先级高于 ``.env``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    """解析简单的 KEY=VALUE 文件。不做变量展开，不支持多行值。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def find_env_file(start: Path | None = None) -> Path | None:
    """从当前目录向上找 .env。"""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        env_file = candidate / ".env"
        if env_file.is_file():
            return env_file
    return None


@dataclass(slots=True)
class Config:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    max_iterations: int = 40
    context_window: int = 131_072
    repeat_limit: int = 3
    workspace_root: Path | None = None
    allow_commands: set[str] = field(
        default_factory=lambda: {
            "python",
            "pytest",
            "ruff",
            "pyright",
            "mypy",
            "eslint",
            "npm",
            "node",
            "go",
            "git",
        }
    )
    temperature: float = 0.2
    timeout: float = 120.0
    #: 是否要求登录才能用工作台。默认关（本机工具，直开即用）；
    #: 置 1 后业务接口一律要求有效会话，未登录返回 401。
    require_auth: bool = False

    @property
    def ready(self) -> bool:
        """没有 api_key 时只能跑离线模式（脚本化模型）。"""
        return bool(self.api_key)


def _truthy(raw: str) -> bool:
    """开关类环境变量的宽松解析：1/true/yes/on 都算开。"""
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def load_config(env_file: Path | None = None) -> Config:
    values = parse_env_file(env_file or find_env_file() or Path(".env"))

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or values.get(key, default)

    def get_int(key: str, default: int) -> int:
        raw = get(key)
        try:
            return int(raw) if raw else default
        except ValueError:
            return default

    def get_float(key: str, default: float) -> float:
        raw = get(key)
        try:
            return float(raw) if raw else default
        except ValueError:
            return default

    commands_raw = get("AUDIT_ALLOW_COMMANDS")
    commands = (
        {item.strip() for item in commands_raw.split(",") if item.strip()}
        if commands_raw
        else None
    )

    workspace_raw = get("AUDIT_WORKSPACE_ROOT")
    config = Config(
        api_key=get("DEEPSEEK_API_KEY"),
        base_url=get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model=get("DEEPSEEK_MODEL", "deepseek-flash"),
        max_iterations=get_int("AUDIT_MAX_ITERATIONS", 40),
        context_window=get_int("AUDIT_CONTEXT_WINDOW", 131_072),
        repeat_limit=get_int("AUDIT_REPEAT_LIMIT", 3),
        workspace_root=Path(workspace_raw).expanduser().resolve() if workspace_raw else None,
        temperature=get_float("AUDIT_TEMPERATURE", 0.2),
        timeout=get_float("AUDIT_TIMEOUT", 120.0),
        require_auth=_truthy(get("MARGIN_REQUIRE_AUTH")),
    )
    if commands:
        config.allow_commands = commands
    return config
