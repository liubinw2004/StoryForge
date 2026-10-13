"""ScriptAgent：把粗糙对话（或已补全剧情）转换为结构化分镜脚本（ScriptDocument）。

- ``run()`` 首参放宽为 ``StoryDocument | str``：
  * 传入 :class:`StoryDocument` → 走「剧情 → 分镜」分支（``script_user_prompt_from_story``）；
  * 传入 ``str`` → 保留旧的「对话 → 分镜」兜底路径（既有调用不破）。
"""

from __future__ import annotations

from typing import Any

from app.agents.base_agent import BaseAgent
from app.models.schemas import Character, Scene, ScriptDocument, Shot, StoryDocument
from app.services.llm_client import LLMError
from app.services.prompt_templates import (
    script_system_prompt,
    script_user_prompt,
    script_user_prompt_from_story,
)
from app.utils.responses import ErrorCode

__all__ = ["ScriptAgent"]


class ScriptAgent(BaseAgent):
    """对话/剧情 → 分镜脚本。"""

    def __init__(self, llm) -> None:
        """初始化。

        Args:
            llm: LLM 客户端。
        """
        super().__init__(llm, name="script_agent")

    def run(
        self,
        source: StoryDocument | str,
        style: str,
        shot_count: int,
        shot_duration: int,
    ) -> ScriptDocument:
        """生成脚本。

        Args:
            source: 已补全的剧情（:class:`StoryDocument`）或粗糙对话文本（``str``）。
            style: 风格代号。
            shot_count: 目标镜头数。
            shot_duration: 单镜头时长（秒）。

        Returns:
            规范化后的 :class:`ScriptDocument`。

        Raises:
            LLMError: 模型未返回合法脚本结构。
        """
        if isinstance(source, StoryDocument):
            user_content = script_user_prompt_from_story(source, style, shot_count, shot_duration)
        else:
            user_content = script_user_prompt(str(source), style, shot_count, shot_duration)

        messages = [
            {"role": "system", "content": script_system_prompt()},
            {"role": "user", "content": user_content},
        ]
        data = self.chat_json(messages, temperature=0.75, timeout=180.0)
        script = self._normalize(data, style, shot_count, shot_duration)
        self.log.info("脚本生成完成：%s，共 %d 个镜头", script.title, len(script.shots))
        return script

    # ------------------------------------------------------------------ #
    def _normalize(
        self,
        data: Any,
        style: str,
        shot_count: int,
        shot_duration: int,
    ) -> ScriptDocument:
        """把模型返回的任意结构规范化为脚本契约。

        Args:
            data: 解析后的对象。
            style: 风格代号。
            shot_count: 目标镜头数。
            shot_duration: 默认单镜头时长。

        Returns:
            规范化后的脚本对象。

        Raises:
            LLMError: 结构无法规范化或镜头为空。
        """
        if isinstance(data, list):
            data = {"shots": data}
        if not isinstance(data, dict):
            raise LLMError(ErrorCode.LLM_BAD_JSON, "模型返回的脚本不是 JSON 对象")

        title = str(data.get("title") or "未命名短剧")
        effective_style = str(data.get("style") or style or "realistic")

        raw_characters = data.get("characters") or []
        characters: list[Character] = []
        if isinstance(raw_characters, list):
            for item in raw_characters:
                if isinstance(item, dict):
                    characters.append(
                        Character(
                            name=str(item.get("name", "")),
                            desc=str(item.get("desc", "")),
                            lock_card=str(item.get("lock_card", "") or ""),
                        )
                    )

        # 场景列表（增量）：index 按数组顺序重排为连续 0..N-1
        raw_scenes = data.get("scenes") or []
        scenes: list[Scene] = []
        if isinstance(raw_scenes, list):
            for position, item in enumerate(raw_scenes):
                if not isinstance(item, dict):
                    continue
                scenes.append(
                    Scene(
                        index=position,
                        name=str(item.get("name", "") or ""),
                        desc=str(item.get("desc", "") or ""),
                        lock_card=str(item.get("lock_card", "") or ""),
                    )
                )
        max_scene_index = max(0, len(scenes) - 1)

        scene = data.get("scene") or ""
        if isinstance(scene, (dict, list)):
            scene = str(scene)
        scene = str(scene)

        raw_positions = data.get("positions") or []
        positions = [str(p) for p in raw_positions] if isinstance(raw_positions, list) else []

        raw_shots = data.get("shots") or []
        if not isinstance(raw_shots, list):
            raw_shots = []
        if shot_count and len(raw_shots) > shot_count:
            raw_shots = raw_shots[:shot_count]

        shots: list[Shot] = []
        for index, item in enumerate(raw_shots, start=1):
            if not isinstance(item, dict):
                continue
            shot_id = str(item.get("shot_id") or f"shot_{index:03d}")
            try:
                duration = int(item.get("duration") or shot_duration)
            except (TypeError, ValueError):
                duration = shot_duration
            duration = max(2, min(10, duration))
            prompt = str(item.get("prompt") or item.get("action") or "")
            scene_index = self._coerce_scene_index(item.get("scene_index"), max_scene_index)
            shots.append(
                Shot(
                    shot_id=shot_id,
                    dialogue=str(item.get("dialogue", "")),
                    action=str(item.get("action", "")),
                    camera=str(item.get("camera", "")),
                    duration=duration,
                    prompt=prompt,
                    scene_index=scene_index,
                )
            )

        if not shots:
            raise LLMError(ErrorCode.LLM_BAD_JSON, "模型返回的脚本中没有任何有效镜头（shots 为空）")

        return ScriptDocument(
            title=title,
            style=effective_style,
            characters=characters,
            scene=scene,
            positions=positions,
            shots=shots,
            scenes=scenes,
        )

    @staticmethod
    def _coerce_scene_index(value, max_scene_index: int) -> int:
        """把 ``scene_index`` 归一化为 ``[0, max_scene_index]`` 内的整数（缺/非法 → 0）。"""
        try:
            index = int(value)
        except (TypeError, ValueError):
            return 0
        if index < 0:
            return 0
        if index > max_scene_index:
            return max_scene_index
        return index
