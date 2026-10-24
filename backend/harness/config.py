"""配置加载。

只读 ``.env`` 与进程环境变量，不引入额外依赖（自己解析，便于审计）。
同一变量名下 **``.env`` 优先于进程环境变量** —— 设置页面写的是 ``.env``，
被环境变量盖掉的话，界面上的保存就「成功但不生效」。

## 模型配置只有三项

只要填 **接口地址 / API Key / 模型名称** 就能接任意 OpenAI 兼容服务
（DeepSeek、OpenAI、Qwen、Moonshot、智谱、vLLM、Ollama、LM Studio、各类中转…）。
协议是业界事实标准，其余的差异（鉴权头、路径、温度范围）都能从这三个值推出来，
不需要再让用户选「提供商」之类的下拉框。

    LLM_BASE_URL=https://api.deepseek.com
    LLM_API_KEY=sk-...
    LLM_MODEL=deepseek-chat

## 旧变量名仍然认

``DEEPSEEK_BASE_URL`` / ``DEEPSEEK_API_KEY`` / ``DEEPSEEK_MODEL`` 继续有效 ——
已经配好的 ``.env`` 不该因为一次重命名就失效。新名字优先。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

#: 建一个全新 ``.env`` 时写进去的接口地址。
#: DeepSeek 是项目一直验证的端点，作为默认值能省掉一次查文档；
#: 换任何别的服务只要改这一个字段。
DEFAULT_BASE_URL = "https://api.deepseek.com"

#: 旧变量名 → 新变量名。读的时候按顺序找，写的时候只写新名字。
LEGACY_KEYS = {
    "LLM_BASE_URL": "DEEPSEEK_BASE_URL",
    "LLM_API_KEY": "DEEPSEEK_API_KEY",
    "LLM_MODEL": "DEEPSEEK_MODEL",
}


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


def normalize_base_url(raw: str) -> str:
    """把用户可能粘进来的各种写法收成可用的前缀。

    用户会贴完整的 ``https://x/v1/chat/completions``，也会贴 ``https://x/v1/``。
    这里统一剥掉尾斜杠与误贴的路径后缀 —— **在客户端拼路径之前**处理，
    否则会请求 ``/chat/completions/chat/completions``。
    """
    url = (raw or "").strip().strip('"').strip("'")
    if not url:
        return ""
    if "://" not in url:
        # 只写了域名：默认 https。本机与内网地址走 http（自建服务通常没证书）。
        host = url.split("/", 1)[0]
        local = re.match(
            r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|192\.168\.|10\.|172\.)",
            host,
        )
        url = ("http://" if local else "https://") + url
    url = url.rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


@dataclass(slots=True)
class Config:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = "deepseek-chat"
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
    #: 工具的调用策略，透传给 API 的 ``tool_choice``。
    #:
    #: 默认 ``auto``（标准语义：模型自己决定）。设成 ``required`` 时**强制**模型每轮必须
    #: 调一个工具 —— 这一项是为「保守型中转」准备的：实测某些中转服务在 ``auto`` 下
    #: 只要指令稍微模糊就不调工具、直接回一句自然语言，导致 Agent 循环第一轮就收尾。
    #: 用 ``required`` 能把它拽回来，但代价是模型无法主动收尾（要靠轮次上限或 finish 工具）。
    tool_choice: str = "auto"
    #: 是否要求登录才能用工作台。默认关（本机工具，直开即用）；
    #: 置 1 后业务接口一律要求有效会话，未登录返回 401。
    require_auth: bool = False

    @property
    def ready(self) -> bool:
        """三项都齐了才能跑真实模型。少一项都会在首次请求时失败，不如提前判断。"""
        return bool(self.api_key and self.base_url and self.model)

    @property
    def missing(self) -> list[str]:
        """还缺哪几项 —— 直接把变量名给出去，用户不用猜。"""
        gaps: list[str] = []
        if not self.base_url:
            gaps.append("LLM_BASE_URL")
        if not self.api_key:
            gaps.append("LLM_API_KEY")
        if not self.model:
            gaps.append("LLM_MODEL")
        return gaps


def _normalize_tool_choice(raw: str) -> str:
    """归一 tool_choice。非法值退回 auto 而不是报错。

    理由：这是个投递参数（tuning knob），写错了最坏结果是「行为和默认一样」，
    不该让整个服务起不来。但要在日志/配置页看得到实际生效值。
    """
    value = (raw or "").strip().lower()
    return value if value in {"auto", "required", "none"} else "auto"


def _truthy(raw: str) -> bool:
    """开关类环境变量的宽松解析：1/true/yes/on 都算开。"""
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_raw(values: dict[str, str], key: str) -> str | None:
    """取「显式写入」的值，没写返回 None。

    与 :func:`load_config` 里的 ``get`` 不同：``get`` 会把空值当成「没配」
    而回退默认；这里要区分两者 —— 用户显式写 ``LLM_MODEL=`` 是明确的意图，
    不该被悄悄换回默认模型。

    ``.env`` 优先于进程环境变量：设置页面存的是 ``.env``，如果环境变量盖过去，
    用户在界面上改的任何值都不会生效 —— 那看起来就是「保存了但没用」。
    """
    for name in (key, LEGACY_KEYS.get(key, "")):
        if not name:
            continue
        # .env 里写了（哪怕写成空串）就以它为准
        if name in values:
            return values[name]
        if name in os.environ:
            return os.environ[name]
    return None



def load_config(env_file: Path | None = None) -> Config:
    values = parse_env_file(env_file or find_env_file() or Path(".env"))

    def get(key: str, default: str = "") -> str:
        """先找新变量名，再找旧变量名。两者都空才用默认值。

        同一名字内部：``.env`` 优先于进程环境变量。设置页面写的是 ``.env``，
        环境变量反过来盖掉它，用户在界面上保存的值就永远不生效 —— 这类
        「保存成功但行为没变」的故障极难自查，所以顺序必须是 .env 在前。
        """
        for name in (key, LEGACY_KEYS.get(key, "")):
            if not name:
                continue
            raw = values.get(name, "") or os.environ.get(name, "")
            if raw:
                return raw
        return default

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
    # 模型名与地址要区分「没写」与「显式写成空」：
    # 没写 → 用默认值（开箱能跑）；写成空 → 尊重用户的空值，并在 missing 里报出来。
    # 否则用户清空模型名想填自己的，却被悄悄换成默认模型，看起来像「配置没生效」。
    base_raw = get("LLM_BASE_URL")
    model_raw = _read_raw(values, "LLM_MODEL")
    config = Config(
        api_key=get("LLM_API_KEY").strip(),
        base_url=normalize_base_url(base_raw if base_raw else DEFAULT_BASE_URL),
        model=(model_raw if model_raw is not None else "deepseek-chat").strip(),
        max_iterations=get_int("AUDIT_MAX_ITERATIONS", 40),
        context_window=get_int("AUDIT_CONTEXT_WINDOW", 131_072),
        repeat_limit=get_int("AUDIT_REPEAT_LIMIT", 3),
        workspace_root=Path(workspace_raw).expanduser().resolve() if workspace_raw else None,
        temperature=get_float("AUDIT_TEMPERATURE", 0.2),
        timeout=get_float("AUDIT_TIMEOUT", 120.0),
        tool_choice=_normalize_tool_choice(get("AUDIT_TOOL_CHOICE")),
        require_auth=_truthy(get("MARGIN_REQUIRE_AUTH")),
    )
    if commands:
        config.allow_commands = commands
    return config
