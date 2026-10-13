"""LLM 客户端：统一封装「OpenAI 兼容接口」与「本地 Ollama」两种模式。

- :class:`OpenAICompatClient`：使用 ``openai`` SDK，``base_url + api_key + model``；
- :class:`OllamaClient`：对话走 ``<base_url>/v1/chat/completions``（openai SDK 指向 ``/v1``，
  api_key 占位 ``"ollama"``），并注入 ``think: false`` 关闭 Qwen3 等模型的思考模式；
  模型列表走 ``httpx`` 请求 ``<base_url>/api/tags``；
- :func:`build_llm_client`：工厂，按 ``cfg.llm_mode`` 返回具体实现。

上层智能体只依赖 :class:`LLMClient` 抽象，不感知底层差异。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

import httpx

from app.models.enums import LLMMode
from app.models.schemas import Config
from app.utils.responses import ErrorCode, err_msg

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "LLMError",
    "LLMClient",
    "OpenAICompatClient",
    "OllamaClient",
    "build_llm_client",
]

# 未显式指定 max_tokens 时使用的默认上限。
# 思考型模型（如 Qwen3）会把思考过程计入生成 token；额度太小会导致
# 思考内容挤空正文（content 为空），故给定一个足够宽裕的默认值。
DEFAULT_MAX_TOKENS: int = 4096


class LLMError(Exception):
    """LLM 调用异常，携带错误码前缀，便于上层生成中文提示。"""

    def __init__(self, code: str, message: str) -> None:
        """初始化异常。

        Args:
            code: 错误码（见 :class:`app.utils.responses.ErrorCode`）。
            message: 中文可读信息。
        """
        self.code = code
        self.message = message
        super().__init__(err_msg(code, message))


class LLMClient(ABC):
    """LLM 统一接口。"""

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float = 60.0,
    ) -> str:
        """发起一次对话补全，返回文本内容。"""

    @abstractmethod
    def list_models(self) -> list[str]:
        """返回可用模型名列表。"""

    @abstractmethod
    def test_connection(self) -> dict:
        """测试连通性。

        Returns:
            ``{"ok": bool, "models": [str], "latency_ms": int, "message": str}``
        """


class OpenAICompatClient(LLMClient):
    """模式A：OpenAI 兼容接口客户端。"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        """初始化。

        Args:
            base_url: 形如 ``https://api.deepseek.com/v1``。
            api_key: 访问密钥（可为空，此时使用占位值）。
            model: 模型名。
        """
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _openai_client(self, timeout: float):
        """构造 openai SDK 客户端。"""
        from openai import OpenAI

        return OpenAI(
            base_url=self.base_url,
            api_key=self.api_key or "sk-no-key",
            timeout=timeout,
        )

    def _classify_error(self, exc: Exception) -> tuple[str, str]:
        """将底层异常分类为 (错误码, 中文信息)。"""
        try:
            import openai
        except Exception:  # pragma: no cover - openai 为必需依赖
            openai = None  # type: ignore[assignment]

        if openai is not None:
            if isinstance(exc, openai.AuthenticationError):
                return ErrorCode.LLM_UNAUTHORIZED, "API Key 无效或未授权，请检查配置"
            if isinstance(exc, openai.NotFoundError):
                return (
                    ErrorCode.LLM_MODEL_NOT_FOUND,
                    f"模型 {self.model} 不存在，请检查模型名或 Base URL 是否正确",
                )
            if isinstance(exc, openai.APITimeoutError):
                return (
                    ErrorCode.LLM_UNREACHABLE,
                    f"请求 {self.base_url} 超时，请检查网络或服务状态",
                )
            if isinstance(exc, openai.APIConnectionError):
                return (
                    ErrorCode.LLM_UNREACHABLE,
                    f"无法连接 {self.base_url}，请确认服务已启动且地址正确",
                )
            if isinstance(exc, openai.APIStatusError):
                status = getattr(exc, "status_code", "?")
                return ErrorCode.LLM_REQUEST_FAILED, f"服务返回错误（HTTP {status}）：{exc}"

        return ErrorCode.LLM_REQUEST_FAILED, f"调用失败：{exc}"

    def _completion_extra_kwargs(self) -> dict:
        """返回附加到 ``chat.completions.create`` 的额外关键字参数。

        默认不附加任何参数；子类（如 :class:`OllamaClient`）可覆写以注入
        厂商私有参数（例如 Ollama 的 ``think``）。

        Returns:
            形如 ``{"extra_body": {...}}`` 的关键字参数字典。
        """
        return {}

    @staticmethod
    def _extract_reasoning(message: object) -> str:
        """从响应消息中提取「思考内容」。

        Ollama 的 OpenAI 兼容接口会把 Qwen3 等思考模型的思考过程放在
        ``reasoning``（或 ``reasoning_content`` / ``thinking``）字段中，
        正文仍在 ``content``。当 ``content`` 为空时借该字段判断是否为
        「思考模式耗尽额度」。

        Args:
            message: openai SDK 返回的 ``ChatCompletionMessage``（或 None）。

        Returns:
            思考内容字符串；不存在则为空串。
        """
        if message is None:
            return ""
        for attr in ("reasoning", "reasoning_content", "thinking"):
            value = getattr(message, attr, None)
            if isinstance(value, str) and value.strip():
                return value
        # openai SDK 的 pydantic 模型把多余字段收在 model_extra 中
        extra = getattr(message, "model_extra", None)
        if isinstance(extra, dict):
            for key in ("reasoning", "reasoning_content", "thinking"):
                value = extra.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return ""

    # ------------------------------------------------------------------ #
    # 公共接口
    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float = 60.0,
    ) -> str:
        """发起一次对话补全。

        Args:
            messages: 对话消息列表。
            temperature: 采样温度。
            max_tokens: 最大生成 token 数；为 ``None`` 或非正数时使用默认值
                :data:`DEFAULT_MAX_TOKENS`（避免思考模型把额度耗尽而返回空正文）。
            timeout: 单次请求超时秒数。

        Returns:
            模型返回的正文文本。

        Raises:
            LLMError: 调用失败（含连接/鉴权/状态码错误）；
                正文为空时：若响应带有思考内容则抛
                :data:`ErrorCode.LLM_CONTENT_EMPTY`，否则抛
                :data:`ErrorCode.LLM_REQUEST_FAILED`。
        """
        client = self._openai_client(timeout)
        effective_max_tokens = (
            max_tokens if (max_tokens and max_tokens > 0) else DEFAULT_MAX_TOKENS
        )
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=effective_max_tokens,
                **self._completion_extra_kwargs(),
            )
        except Exception as exc:
            code, message = self._classify_error(exc)
            raise LLMError(code, message) from exc

        choices = getattr(response, "choices", None) or []
        raw_message = getattr(choices[0], "message", None) if choices else None
        content = getattr(raw_message, "content", None) or ""
        if content and content.strip():
            return content

        # 正文为空：区分「思考模式耗尽额度」与「确实返回空内容」两种情况，
        # 给出可操作的中文提示。
        if self._extract_reasoning(raw_message):
            raise LLMError(
                ErrorCode.LLM_CONTENT_EMPTY,
                "模型处于思考模式且未产出正文（请关闭 think 或增大 max_tokens）",
            )
        raise LLMError(ErrorCode.LLM_REQUEST_FAILED, "模型返回了空内容")

    def list_models(self) -> list[str]:
        """调用 ``/v1/models`` 列出模型。"""
        client = self._openai_client(30.0)
        try:
            data = client.models.list()
        except Exception as exc:
            code, message = self._classify_error(exc)
            raise LLMError(code, message) from exc
        items = getattr(data, "data", None) or []
        return sorted({getattr(item, "id", "") for item in items if getattr(item, "id", "")})

    def test_connection(self) -> dict:
        """测试连通性并测量延迟。"""
        started = time.perf_counter()
        try:
            models = self.list_models()
            latency = int((time.perf_counter() - started) * 1000)
            return {
                "ok": True,
                "models": models,
                "latency_ms": latency,
                "message": f"连接成功，检测到 {len(models)} 个模型",
            }
        except LLMError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            return {"ok": False, "models": [], "latency_ms": latency, "message": str(exc)}
        except Exception as exc:  # 兜底，不向调用方抛异常
            latency = int((time.perf_counter() - started) * 1000)
            return {
                "ok": False,
                "models": [],
                "latency_ms": latency,
                "message": err_msg(ErrorCode.LLM_REQUEST_FAILED, str(exc)),
            }


class OllamaClient(OpenAICompatClient):
    """模式B：本地 Ollama 客户端。

    - 对话走 OpenAI 兼容端点 ``<base_url>/v1/chat/completions``，并**显式关闭
      思考模式**（``think: false``），保证 ``content`` 直接给出正文；
    - 模型列表走 ``<base_url>/api/tags``。
    """

    def __init__(self, base_url: str, model: str) -> None:
        """初始化。

        Args:
            base_url: Ollama 主机地址，如 ``http://localhost:11434``。
            model: 模型名，如 ``qwen3:30b-a3b``。
        """
        host = (base_url or "http://localhost:11434").rstrip("/")
        if host.endswith("/v1"):
            host = host[: -len("/v1")]
        self.host = host
        super().__init__(base_url=host + "/v1", api_key="ollama", model=model)

    def _completion_extra_kwargs(self) -> dict:
        """Ollama 模式：显式关闭思考模式。

        Qwen3 等模型默认开启思考，思考内容会占用 ``max_tokens`` 且只出现在
        ``reasoning`` 字段——若额度被思考吃光，``content`` 会为空，上层将
        拿到「模型返回了空内容」。通过 Ollama 私有的 ``think`` 参数关闭后，
        正文直接进入 ``content``。

        Returns:
            ``{"extra_body": {"think": False}}``。
        """
        return {"extra_body": {"think": False}}

    def list_models(self) -> list[str]:
        """请求 ``/api/tags`` 列出本地模型。"""
        url = f"{self.host}/api/tags"
        try:
            response = httpx.get(url, timeout=10.0)
            response.raise_for_status()
        except httpx.ConnectError as exc:
            raise LLMError(
                ErrorCode.LLM_UNREACHABLE,
                f"无法连接 {self.host}，请确认 Ollama 已启动（可执行 ollama serve）",
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMError(
                ErrorCode.LLM_UNREACHABLE,
                f"请求 {url} 超时，请确认 Ollama 服务状态",
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                ErrorCode.LLM_REQUEST_FAILED,
                f"Ollama 返回异常状态码 {exc.response.status_code}",
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(ErrorCode.LLM_UNREACHABLE, f"请求 {url} 失败：{exc}") from exc

        try:
            payload = response.json()
        except Exception as exc:
            raise LLMError(
                ErrorCode.LLM_REQUEST_FAILED,
                "Ollama 返回内容无法解析为 JSON",
            ) from exc

        models = [
            item.get("name", "")
            for item in (payload.get("models") or [])
            if isinstance(item, dict) and item.get("name")
        ]
        return models


def build_llm_client(cfg: Config) -> LLMClient:
    """工厂：按 ``cfg.llm_mode`` 返回具体 LLM 客户端。

    Args:
        cfg: 配置对象。

    Returns:
        :class:`OllamaClient` 或 :class:`OpenAICompatClient`。
    """
    if cfg.llm_mode == LLMMode.OLLAMA:
        return OllamaClient(base_url=cfg.llm_base_url, model=cfg.llm_model)
    return OpenAICompatClient(
        base_url=cfg.llm_base_url,
        api_key=cfg.llm_api_key,
        model=cfg.llm_model,
    )
