"""模型接入层。

只依赖 httpx；请求体按 **OpenAI 兼容协议**构造 —— 这是业界事实标准，
DeepSeek / OpenAI / Qwen / Moonshot / 智谱 / vLLM / Ollama / 各类中转都实现它。
所以模型层不绑定任何一家：给三个值（接口地址、Key、模型名）就能跑。

两条实现：
- ``OpenAIModel`` 真实调用 API，带可重试错误的指数退避。
- ``ScriptedModel`` 按预设脚本返回，用于离线测试与流程验证（无需 API Key）。

注意一个容易踩的坑：DeepSeek 的思考内容走 ``reasoning_content`` 字段，
**不能回传**到后续请求的 messages 里（官方明确要求），
所以 ``Decision.to_message()`` 只带 content 与 tool_calls。
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from .config import Config
from .types import Decision, Message, ToolCall, Usage


class ModelError(RuntimeError):
    """模型调用失败。``retryable`` 决定上层是重试还是直接熔断。"""

    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        suggestion: str = "",
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.status = status
        self.retryable = retryable
        self.suggestion = suggestion


class Model(Protocol):
    name: str

    def complete(
        self,
        messages: Sequence[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> Decision: ...


# =============================================================================
# OpenAI 兼容端点
# =============================================================================

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAIModel:
    """任意 OpenAI 兼容端点（/chat/completions）。"""

    def __init__(self, config: Config, *, max_retries: int = 3) -> None:
        if not config.api_key:
            raise ModelError(
                "MissingAPIKey",
                "未配置 LLM_API_KEY，无法调用真实模型。",
                suggestion="在 backend/.env 里填入 LLM_API_KEY，或在配置中心填写；想先验证链路可加 --offline。",
            )
        self.config = config
        self.name = config.model
        self.max_retries = max_retries
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        )

    # ------------------------------------------------------------------ #

    def complete(
        self,
        messages: Sequence[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> Decision:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [message.to_api() for message in messages],
            "temperature": self.config.temperature if temperature is None else temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        last_error: ModelError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post("/chat/completions", json=payload)
            except httpx.TimeoutException as exc:
                last_error = ModelError(
                    "Timeout",
                    f"请求超时（{self.config.timeout}s）。",
                    retryable=True,
                    suggestion="长上下文首 token 可能很慢，可调大 AUDIT_TIMEOUT。",
                )
                last_error.__cause__ = exc
            except httpx.HTTPError as exc:
                last_error = ModelError(
                    "NetworkError",
                    f"网络请求失败：{exc}",
                    retryable=True,
                    suggestion="检查出网代理（HTTPS_PROXY）与 base_url 是否可达。",
                )
            else:
                if response.status_code == 200:
                    return self._parse(response.json())
                last_error = self._classify(response)

            if not last_error.retryable or attempt == self.max_retries:
                raise last_error
            time.sleep(min(2**attempt, 8))

        raise last_error or ModelError("Unknown", "模型调用失败")

    # ------------------------------------------------------------------ #

    def _classify(self, response: httpx.Response) -> ModelError:
        status = response.status_code
        body = response.text[:400]
        mapping = {
            400: ("BadRequest", "请求格式被拒绝。", "检查工具 schema 与消息结构是否合法。"),
            401: ("AuthError", "API Key 无效或与接���不匹配。", "核对 LLM_API_KEY：多数服务要求 sk- 开头、且不能有首尾空格。"),
            402: ("InsufficientBalance", "账户余额不足。", "到服务商控制台充值后重试。"),
            403: ("Forbidden", "该密钥无权访问此模型。", "确认模型名在账号可用列表内。"),
            404: ("ModelNotFound", "接口路径或模型名不存在。", "核对 LLM_BASE_URL 是否以 /v1 结尾、LLM_MODEL 是否是服务端确实提供的名字。"),
            422: ("UnprocessableEntity", "参数不被接受。", "检查 temperature 等参数取值范围。"),
            429: ("RateLimit", "触发限流。", "降低并发或稍后重试。"),
        }
        error_type, message, suggestion = mapping.get(
            status, ("ServerError", "服务端返回异常。", "稍后重试；持续失败请查看服务状态页。")
        )
        return ModelError(
            error_type,
            f"{message} HTTP {status}：{body}",
            status=status,
            retryable=status in _RETRYABLE_STATUS,
            suggestion=suggestion,
        )

    def _parse(self, data: dict[str, Any]) -> Decision:
        choices = data.get("choices") or []
        if not choices:
            raise ModelError("EmptyResponse", "模型返回了空的 choices。", retryable=True)

        choice = choices[0]
        message = choice.get("message") or {}

        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            calls.append(ToolCall.from_api(raw))

        usage_raw = data.get("usage") or {}
        # 缓存命中字段各家不同：DeepSeek 走 prompt_cache_hit_tokens，
        # OpenAI 这类走 prompt_tokens_details.cached_tokens，两种都认。
        cached = usage_raw.get("prompt_cache_hit_tokens")
        if cached is None:
            cached = (usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)

        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
            cached_tokens=int(cached or 0),
            total_tokens=int(usage_raw.get("total_tokens") or 0),
        )

        return Decision(
            # 思考内容只用于展示与日志，不回传上下文
            reasoning=str(message.get("reasoning_content") or ""),
            content=str(message.get("content") or ""),
            tool_calls=calls,
            usage=usage,
            finish_reason=str(choice.get("finish_reason") or ""),
        )

    def close(self) -> None:
        self._client.close()


# =============================================================================
# 离线脚本模型
# =============================================================================


class ScriptedModel:
    """按脚本回放决策，用于测试与无 Key 演示。

    脚本元素可以是 ``Decision``，也可以是一个函数
    ``(messages, tools) -> Decision``，后者便于根据工具结果编排分支。
    """

    def __init__(self, script: Sequence[Decision | Any], name: str = "scripted") -> None:
        self._script = list(script)
        self._cursor = 0
        self.name = name
        self.calls = 0

    def complete(
        self,
        messages: Sequence[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> Decision:
        self.calls += 1
        if self._cursor >= len(self._script):
            # 脚本用尽：给一个明确终止的答复，避免死循环
            return Decision(content="（脚本已用尽，结束本次运行）")

        item = self._script[self._cursor]
        self._cursor += 1
        if callable(item):
            return item(messages, tools)
        return item


