"""三字段模型配置的测试。

接模型只需要 **接口地址 / API Key / 模型名称**，协议固定是 OpenAI 兼容。
这些测试钉住三件事：

1. 三个新变量名能读到，**旧变量名也还能读到**（已配好的 .env 不该失效）；
2. 用户粘贴的各种地址写法都能收成可用前缀（否则会请求
   ``/chat/completions/chat/completions``，看起来像「服务挂了」）；
3. 三项缺任何一项都不能声称可用。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import (
    DEFAULT_BASE_URL,
    LEGACY_KEYS,
    Config,
    load_config,
    normalize_base_url,
)


def _env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def _clean(monkeypatch) -> None:
    """清掉进程环境里的相关变量，免得本机真实配置干扰断言。"""
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", *LEGACY_KEYS.values()):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# 三项
# --------------------------------------------------------------------------- #


def test_reads_the_three_fields(tmp_path: Path, monkeypatch) -> None:
    _clean(monkeypatch)
    env = _env(
        tmp_path,
        "LLM_BASE_URL=https://api.moonshot.cn/v1\n"
        "LLM_API_KEY=sk-moon\n"
        "LLM_MODEL=moonshot-v1-32k\n",
    )
    config = load_config(env)
    assert config.base_url == "https://api.moonshot.cn/v1"
    assert config.api_key == "sk-moon"
    assert config.model == "moonshot-v1-32k"
    assert config.ready is True
    assert config.missing == []


def test_legacy_variable_names_still_work(tmp_path: Path, monkeypatch) -> None:
    """已经配好的 .env 不该因为一次重命名就失效。"""
    _clean(monkeypatch)
    env = _env(
        tmp_path,
        "DEEPSEEK_BASE_URL=https://legacy.example/v1\n"
        "DEEPSEEK_API_KEY=sk-old\n"
        "DEEPSEEK_MODEL=legacy-model\n",
    )
    config = load_config(env)
    assert config.base_url == "https://legacy.example/v1"
    assert config.api_key == "sk-old"
    assert config.model == "legacy-model"
    assert config.ready is True


def test_new_name_wins_over_legacy(tmp_path: Path, monkeypatch) -> None:
    _clean(monkeypatch)
    env = _env(
        tmp_path,
        "LLM_MODEL=new-model\nDEEPSEEK_MODEL=old-model\n",
    )
    assert load_config(env).model == "new-model"


def test_file_wins_over_process_env(tmp_path: Path, monkeypatch) -> None:
    """`.env` 优先于进程环境变量。

    设置页面存的是 ``.env``。如果环境变量盖过去，用户在界面上保存的值永远
    不生效 —— 「保存成功但行为没变」这类故障极难自查，所以顺序必须是
    .env 在前。环境变量只在 .env 没写这一项时兜底。
    """
    _clean(monkeypatch)
    env = _env(tmp_path, "LLM_MODEL=from-file\n")
    monkeypatch.setenv("LLM_MODEL", "from-env")
    assert load_config(env).model == "from-file"


def test_process_env_fills_gap(tmp_path: Path, monkeypatch) -> None:
    """.env 没写的那一项，环境变量兜底（不破坏已有部署）。"""
    _clean(monkeypatch)
    env = _env(tmp_path, "LLM_API_KEY=k\n")
    monkeypatch.setenv("LLM_MODEL", "from-env")
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example/v1")
    config = load_config(env)
    assert config.model == "from-env"
    assert config.base_url == "https://env.example/v1"
    assert config.api_key == "k"


@pytest.mark.parametrize(
    "body, missing",
    [
        ("LLM_BASE_URL=https://x/v1\nLLM_MODEL=m\n", ["LLM_API_KEY"]),
        ("LLM_API_KEY=k\nLLM_MODEL=m\n", []),  # base_url 有默认值
        ("LLM_API_KEY=k\nLLM_BASE_URL=https://x/v1\n", []),  # model 有默认值
        ("LLM_API_KEY=\nLLM_BASE_URL=https://x/v1\nLLM_MODEL=\n", ["LLM_API_KEY", "LLM_MODEL"]),
    ],
)
def test_missing_fields_are_named(
    tmp_path: Path, monkeypatch, body: str, missing: list[str]
) -> None:
    """缺项要指名到变量级。只说「未配置」等于让用户猜。"""
    _clean(monkeypatch)
    config = load_config(_env(tmp_path, body))
    assert config.missing == missing
    assert config.ready is (not missing)


def test_empty_key_is_not_ready() -> None:
    assert Config(api_key="", base_url="https://x/v1", model="m").ready is False


def test_default_base_url_is_usable() -> None:
    assert normalize_base_url(DEFAULT_BASE_URL) == DEFAULT_BASE_URL


# --------------------------------------------------------------------------- #
# 地址归一化
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://api.deepseek.com", "https://api.deepseek.com"),
        ("https://api.deepseek.com/", "https://api.deepseek.com"),
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        # 贴成完整请求地址是最常见的误操作
        ("https://api.openai.com/v1/chat/completions", "https://api.openai.com/v1"),
        ("https://api.x.com/v1/models", "https://api.x.com/v1"),
        # 只写域名：默认 https
        ("api.moonshot.cn/v1", "https://api.moonshot.cn/v1"),
        # 本机与内网：默认 http（自建服务通常没证书）
        ("localhost:11434/v1", "http://localhost:11434/v1"),
        ("127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1"),
        ("192.168.1.50:8000/v1", "http://192.168.1.50:8000/v1"),
        # 首尾空格与引号
        ('  "https://x.com/v1/"  ', "https://x.com/v1"),
        ("", ""),
    ],
)
def test_normalize_base_url(raw: str, expected: str) -> None:
    assert normalize_base_url(raw) == expected


def test_normalize_strips_only_one_suffix() -> None:
    """只剥一层后缀：路径里的其它段是用户自己的，不该被改。"""
    assert (
        normalize_base_url("https://gw.example/openai/v1/chat/completions")
        == "https://gw.example/openai/v1"
    )


# --------------------------------------------------------------------------- #
# tool_choice：保守型中转的适配开关
# --------------------------------------------------------------------------- #


def test_tool_choice_defaults_to_auto(tmp_path: Path, monkeypatch) -> None:
    """没写就是标准语义 auto。"""
    _clean(monkeypatch)
    monkeypatch.delenv("AUDIT_TOOL_CHOICE", raising=False)
    config = load_config(_env(tmp_path, "LLM_API_KEY=k\n"))
    assert config.tool_choice == "auto"


def test_tool_choice_accepts_required(tmp_path: Path, monkeypatch) -> None:
    """``required`` 是为「auto 下不调工具」的保守中转准备的。"""
    _clean(monkeypatch)
    monkeypatch.delenv("AUDIT_TOOL_CHOICE", raising=False)
    config = load_config(_env(tmp_path, "LLM_API_KEY=k\nAUDIT_TOOL_CHOICE=required\n"))
    assert config.tool_choice == "required"


def test_tool_choice_is_case_insensitive(tmp_path: Path, monkeypatch) -> None:
    _clean(monkeypatch)
    monkeypatch.delenv("AUDIT_TOOL_CHOICE", raising=False)
    config = load_config(_env(tmp_path, "LLM_API_KEY=k\nAUDIT_TOOL_CHOICE=REQUIRED\n"))
    assert config.tool_choice == "required"


def test_bad_tool_choice_falls_back_to_auto(tmp_path: Path, monkeypatch) -> None:
    """非法值退回 auto，而不是让服务起不来。

    这是个投递参数（tuning knob），写错最坏结果是「跟默认一样」，
    不该变成启动失败。
    """
    _clean(monkeypatch)
    monkeypatch.delenv("AUDIT_TOOL_CHOICE", raising=False)
    config = load_config(_env(tmp_path, "LLM_API_KEY=k\nAUDIT_TOOL_CHOICE=bogus\n"))
    assert config.tool_choice == "auto"


# --------------------------------------------------------------------------- #
# 本机端点不能被系统代理劫持
# --------------------------------------------------------------------------- #


def test_model_client_ignores_system_proxy(tmp_path: Path, monkeypatch) -> None:
    """模型客户端必须 ``trust_env=False``。

    实测事故：base_url 指向本机网关（``http://127.0.0.1:7864/v1``）时，
    httpx 默认读取 ``HTTP_PROXY`` 与 Windows 系统代理，把请求送到代理，
    代理再转发回来就是 **502**。而命令行 curl 加 ``--noproxy`` 完全正常 ——
    两边行为对不上，排查成本极高。

    这条测试守的是「不许有人为了『兼容企业代理』把这个开关改回默认」。
    """
    from harness.model import OpenAIModel

    _clean(monkeypatch)
    env = _env(tmp_path, "LLM_API_KEY=k\nLLM_BASE_URL=http://127.0.0.1:7864/v1\nLLM_MODEL=m\n")
    config = load_config(env)
    model = OpenAIModel(config)
    assert model._client._trust_env is False


# --------------------------------------------------------------------------- #
# tool_choice：保守型中转的适配开关
# --------------------------------------------------------------------------- #


def test_tool_choice_defaults_to_auto(tmp_path: Path, monkeypatch) -> None:
    """没写就是标准语义 auto。"""
    _clean(monkeypatch)
    monkeypatch.delenv("AUDIT_TOOL_CHOICE", raising=False)
    config = load_config(_env(tmp_path, "LLM_API_KEY=k\n"))
    assert config.tool_choice == "auto"


def test_tool_choice_accepts_required(tmp_path: Path, monkeypatch) -> None:
    """``required`` 是为「auto 下不调工具」的保守中转准备的。"""
    _clean(monkeypatch)
    monkeypatch.delenv("AUDIT_TOOL_CHOICE", raising=False)
    config = load_config(_env(tmp_path, "LLM_API_KEY=k\nAUDIT_TOOL_CHOICE=required\n"))
    assert config.tool_choice == "required"


def test_tool_choice_is_case_insensitive(tmp_path: Path, monkeypatch) -> None:
    _clean(monkeypatch)
    monkeypatch.delenv("AUDIT_TOOL_CHOICE", raising=False)
    config = load_config(_env(tmp_path, "LLM_API_KEY=k\nAUDIT_TOOL_CHOICE=REQUIRED\n"))
    assert config.tool_choice == "required"


def test_bad_tool_choice_falls_back_to_auto(tmp_path: Path, monkeypatch) -> None:
    """非法值退回 auto，而不是让服务起不来。

    这是个投递参数（tuning knob），写错最坏结果是「跟默认一样」，
    不该变成启动失败。
    """
    _clean(monkeypatch)
    monkeypatch.delenv("AUDIT_TOOL_CHOICE", raising=False)
    config = load_config(_env(tmp_path, "LLM_API_KEY=k\nAUDIT_TOOL_CHOICE=bogus\n"))
    assert config.tool_choice == "auto"


# --------------------------------------------------------------------------- #
# 本机端点不能被系统代理劫持
# --------------------------------------------------------------------------- #


def test_model_client_ignores_system_proxy(tmp_path: Path, monkeypatch) -> None:
    """模型客户端必须 ``trust_env=False``。

    实测事故：base_url 指向本机网关（``http://127.0.0.1:7864/v1``）时，
    httpx 默认读取 ``HTTP_PROXY`` 与 Windows 系统代理，把请求送到代理，
    代理再转发回来就是 **502**。而命令行 curl 加 ``--noproxy`` 完全正常 ——
    两边行为对不上，排查成本极高。

    这条测试守的是「不许有人为了『兼容企业代理』把这个开关改回默认」。
    """
    from harness.model import OpenAIModel

    _clean(monkeypatch)
    env = _env(tmp_path, "LLM_API_KEY=k\nLLM_BASE_URL=http://127.0.0.1:7864/v1\nLLM_MODEL=m\n")
    config = load_config(env)
    model = OpenAIModel(config)
    assert model._client._trust_env is False
