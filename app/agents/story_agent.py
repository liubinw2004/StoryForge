"""StoryAgent：把粗糙对话补全为完整故事情节（StoryDocument）。

设计要点：
- 输入一段粗糙对话 + 风格，输出承载「完整故事情节」的 :class:`StoryDocument`；
- 复用 :class:`BaseAgent.chat_json` 的「强约束 JSON + 自动重试」能力；
- 失败处理沿用既有约定：连续失败抛 ``LLMError(LLM_BAD_JSON, …)``，由
  ``TaskManager.run_pipeline`` 统一置任务 ``failed``；**不新增错误码**；
- ``_normalize`` 对非 dict、``synopsis`` 为空、``beats`` 为空视为无效结构；
- ``beats`` 按数组顺序重排 ``index=1..N``，容忍小模型漏字段/乱填序号。
"""

from __future__ import annotations

from typing import Any

from app.agents.base_agent import BaseAgent
from app.models.schemas import Character, StoryBeat, StoryDocument
from app.services.llm_client import LLMError
from app.services.prompt_templates import (
    story_system_prompt,
    story_user_prompt,
    style_label,
)
from app.utils.responses import ErrorCode

__all__ = ["StoryAgent"]


class StoryAgent(BaseAgent):
    """粗糙对话 → 完整故事情节。"""

    def __init__(self, llm) -> None:
        """初始化。

        Args:
            llm: LLM 客户端。
        """
        super().__init__(llm, name="story_agent")

    def run(self, dialogue: str, style: str) -> StoryDocument:
        """补全故事情节。

        Args:
            dialogue: 粗糙对话文本。
            style: 风格代号。

        Returns:
            规范化后的 :class:`StoryDocument`。

        Raises:
            LLMError: 模型未返回合法剧情结构。
        """
        messages = [
            {"role": "system", "content": story_system_prompt()},
            {"role": "user", "content": story_user_prompt(dialogue, style)},
        ]
        data = self.chat_json(messages, temperature=0.85, timeout=180.0)
        story = self._normalize(data, dialogue, style)
        self.log.info("剧情补全完成：%s，共 %d 个情节点", story.title, len(story.beats))
        return story

    # ------------------------------------------------------------------ #
    def _normalize(self, data: Any, dialogue: str, style: str) -> StoryDocument:
        """把模型返回的任意结构规范化为剧情契约。

        Args:
            data: 解析后的对象。
            dialogue: 原始粗糙对话（写入 ``source_dialogue`` 溯源）。
            style: 风格代号（用于推导缺省的 ``genre``）。

        Returns:
            规范化后的剧情对象。

        Raises:
            LLMError: 结构无法规范化、``synopsis`` 为空或 ``beats`` 为空。
        """
        if isinstance(data, list):
            data = {"beats": data}
        if not isinstance(data, dict):
            raise LLMError(ErrorCode.LLM_BAD_JSON, "模型返回的剧情不是 JSON 对象")

        title = str(data.get("title") or "未命名故事")
        logline = str(data.get("logline") or "")

        synopsis = data.get("synopsis") or ""
        if isinstance(synopsis, (dict, list)):
            synopsis = str(synopsis)
        synopsis = str(synopsis).strip()
        if not synopsis:
            raise LLMError(ErrorCode.LLM_BAD_JSON, "模型返回的剧情结构无效：synopsis 为空")

        setting = data.get("setting") or ""
        if isinstance(setting, (dict, list)):
            setting = str(setting)
        setting = str(setting)

        # 角色表（复用 Character{name, desc}）
        raw_characters = data.get("characters") or []
        characters: list[Character] = []
        if isinstance(raw_characters, list):
            for item in raw_characters:
                if isinstance(item, dict):
                    characters.append(
                        Character(
                            name=str(item.get("name", "")),
                            desc=str(item.get("desc", "")),
                        )
                    )

        # 情节节点：按数组顺序重排 index=1..N（容忍小模型漏字段）
        raw_beats = data.get("beats") or []
        if not isinstance(raw_beats, list):
            raw_beats = []
        beats: list[StoryBeat] = []
        for order, item in enumerate(raw_beats, start=1):
            if not isinstance(item, dict):
                continue
            beats.append(
                StoryBeat(
                    index=order,
                    summary=str(item.get("summary", "")),
                    purpose=str(item.get("purpose", "")),
                    location=str(item.get("location", "")),
                )
            )
        if not beats:
            raise LLMError(ErrorCode.LLM_BAD_JSON, "模型返回的剧情结构无效：beats 为空")
        for position, beat in enumerate(beats, start=1):
            beat.index = position

        genre = str(data.get("genre") or "")
        if not genre and style:
            genre = style_label(style)
        theme = str(data.get("theme") or "")

        extra = data.get("extra")
        if not isinstance(extra, dict):
            extra = {}

        return StoryDocument(
            title=title,
            logline=logline,
            synopsis=synopsis,
            setting=setting,
            characters=characters,
            beats=beats,
            genre=genre,
            theme=theme,
            source_dialogue=dialogue,
            extra=extra,
        )
