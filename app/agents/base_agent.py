"""智能体基类：持有 LLM 客户端，提供「强约束 JSON 对话 + 自动重试」能力。"""

from __future__ import annotations

from abc import ABC
from typing import Any

from app.services.json_parser import parse_json_lenient
from app.services.llm_client import LLMClient, LLMError
from app.services.prompt_templates import json_retry_prompt
from app.utils.logger import get_logger
from app.utils.responses import ErrorCode

__all__ = ["BaseAgent"]


class BaseAgent(ABC):
    """所有智能体的抽象基类。"""

    def __init__(self, llm: LLMClient, *, name: str = "agent") -> None:
        """初始化。

        Args:
            llm: LLM 客户端（不感知具体实现）。
            name: 日志模块名。
        """
        self.llm = llm
        self.name = name
        self.log = get_logger(name)

    # ------------------------------------------------------------------ #
    def chat_json(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        retries: int = 2,
        timeout: float = 120.0,
    ) -> Any:
        """发起对话并解析为 JSON，失败自动重试。

        Args:
            messages: 对话消息列表。
            temperature: 采样温度。
            max_tokens: 最大生成 token 数。
            retries: 解析失败后的额外重试次数（默认 2）。
            timeout: 单次请求超时秒数。

        Returns:
            解析得到的对象。

        Raises:
            LLMError: 连续 ``retries+1`` 次均未能获得合法 JSON。
        """
        attempt = 0
        last_error: Exception | None = None
        conversation = list(messages)

        while attempt <= retries:
            content = self.llm.chat(
                conversation,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            try:
                return parse_json_lenient(content)
            except ValueError as exc:
                last_error = exc
                attempt += 1
                if attempt > retries:
                    break
                self.log.warning("%s JSON 解析失败，第 %d 次重试", self.name, attempt)
                conversation = list(messages) + [
                    {"role": "assistant", "content": content[:4000]},
                    {"role": "user", "content": json_retry_prompt()},
                ]

        raise LLMError(
            ErrorCode.LLM_BAD_JSON,
            f"模型连续 {retries + 1} 次未返回合法 JSON（{last_error}）。可尝试更换模型或稍后重试",
        )
