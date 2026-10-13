"""增量补充测试：``StoryAgent._normalize`` 失败路径，及 ``ScriptAgent`` str 兜底回归。

对应设计 §2.2 `_normalize` 契约与 §2.4「``run()`` 首参放宽为 ``StoryDocument | str``，
保留 str 兜底，旧调用不破」。

运行：``.venv/Scripts/python.exe -m unittest tests.test_story_agent -v``
"""

from __future__ import annotations

import json
import unittest

from app.agents.script_agent import ScriptAgent
from app.agents.story_agent import StoryAgent
from app.models.schemas import ScriptDocument, Shot, StoryDocument
from app.services.llm_client import LLMClient, LLMError
from app.utils.responses import ErrorCode

VALID_STORY = {
    "title": "咖啡馆的约定",
    "logline": "两人重逢，决定一起创业",
    "synopsis": "第一段起因。\n\n第二段转折与收尾。",
    "setting": "午后咖啡馆",
    "characters": [{"name": "小明", "desc": "短发男生"}],
    "beats": [
        {"index": 1, "summary": "重逢", "purpose": "铺垫", "location": "门口"},
        {"index": 2, "summary": "决定创业", "purpose": "收尾", "location": "座位"},
    ],
    "genre": "都市情感",
    "theme": "友情与梦想",
}

VALID_SCRIPT = {
    "title": "咖啡馆的约定",
    "style": "realistic",
    "shots": [
        {"shot_id": "shot_001", "dialogue": "你好", "action": "推门", "duration": 4, "prompt": "p1"},
        {"shot_id": "shot_002", "dialogue": "再见", "action": "转身", "duration": 4, "prompt": "p2"},
    ],
}


class FakeLLMClient(LLMClient):
    """按预设脚本依次返回文本的假客户端（不联网）。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls: list[list[dict]] = []

    def chat(self, messages, **kwargs) -> str:  # noqa: D102
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[index]

    def list_models(self) -> list[str]:  # noqa: D102
        return []

    def test_connection(self) -> dict:  # noqa: D102
        return {"ok": True, "models": [], "latency_ms": 0, "message": ""}


class TestStoryNormalizeFailures(unittest.TestCase):
    """``_normalize`` 三类非法结构必须抛 ``LLMError(LLM_BAD_JSON)``。"""

    def setUp(self) -> None:
        self.agent = StoryAgent(FakeLLMClient(["unused"]))

    def test_non_dict_raises(self) -> None:
        for bad in ("一段普通文本", 123, None, 3.14):
            with self.assertRaises(LLMError) as ctx:
                self.agent._normalize(bad, "对话", "realistic")
            self.assertEqual(ctx.exception.code, ErrorCode.LLM_BAD_JSON)
            self.assertIn("不是 JSON 对象", ctx.exception.message)

    def test_empty_synopsis_raises(self) -> None:
        for empty in ("", "   ", None):
            data = {"synopsis": empty, "beats": [{"summary": "a"}]}
            with self.assertRaises(LLMError) as ctx:
                self.agent._normalize(data, "对话", "realistic")
            self.assertEqual(ctx.exception.code, ErrorCode.LLM_BAD_JSON)
            self.assertIn("synopsis", ctx.exception.message)

    def test_empty_beats_raises(self) -> None:
        for beats in ([], None, "not-a-list"):
            data = {"synopsis": "有正文", "beats": beats}
            with self.assertRaises(LLMError) as ctx:
                self.agent._normalize(data, "对话", "realistic")
            self.assertEqual(ctx.exception.code, ErrorCode.LLM_BAD_JSON)
            self.assertIn("beats", ctx.exception.message)

    def test_beats_index_reindexed_to_1_to_n(self) -> None:
        data = dict(VALID_STORY)
        data["beats"] = [
            {"index": 9, "summary": "a"},
            {"index": 5, "summary": "b"},
            {"index": 1, "summary": "c"},
        ]
        story = self.agent._normalize(data, "对话", "realistic")
        self.assertEqual([beat.index for beat in story.beats], [1, 2, 3])
        self.assertEqual([beat.summary for beat in story.beats], ["a", "b", "c"])

    def test_genre_derived_from_style_when_absent(self) -> None:
        data = {k: v for k, v in VALID_STORY.items() if k != "genre"}
        story = self.agent._normalize(data, "对话", "anime")
        self.assertEqual(story.genre, "动漫")

    def test_source_dialogue_recorded(self) -> None:
        story = self.agent._normalize(VALID_STORY, "原始对话内容", "realistic")
        self.assertEqual(story.source_dialogue, "原始对话内容")


class TestStoryAgentRun(unittest.TestCase):
    """``run()`` 端到端（FakeLLM）出参与错误传播。"""

    def test_run_returns_story_document(self) -> None:
        llm = FakeLLMClient([json.dumps(VALID_STORY, ensure_ascii=False)])
        story = StoryAgent(llm).run("对话", "realistic")
        self.assertIsInstance(story, StoryDocument)
        self.assertTrue(story.synopsis)
        self.assertGreaterEqual(len(story.beats), 1)
        self.assertEqual(len(llm.calls), 1)

    def test_run_invalid_json_retries_then_raises(self) -> None:
        llm = FakeLLMClient(["不是 JSON", "还是不是", "依然不是"])
        with self.assertRaises(LLMError) as ctx:
            StoryAgent(llm).run("对话", "realistic")
        self.assertEqual(ctx.exception.code, ErrorCode.LLM_BAD_JSON)
        self.assertEqual(len(llm.calls), 3)  # retries=2 → 共 3 次

    def test_run_structurally_invalid_json_raises(self) -> None:
        """JSON 合法但结构非法（synopsis 空）→ 同样抛 LLM_BAD_JSON，且不重试。"""
        llm = FakeLLMClient([json.dumps({"title": "t", "beats": []})])
        with self.assertRaises(LLMError) as ctx:
            StoryAgent(llm).run("对话", "realistic")
        self.assertEqual(ctx.exception.code, ErrorCode.LLM_BAD_JSON)
        self.assertEqual(len(llm.calls), 1)


class TestScriptAgentStrFallbackRegression(unittest.TestCase):
    """旧调用不破：``run(str, ...)`` 仍返回 ``ScriptDocument``。"""

    def test_str_source_still_works(self) -> None:
        llm = FakeLLMClient([json.dumps(VALID_SCRIPT, ensure_ascii=False)])
        script = ScriptAgent(llm).run("一段对话", "realistic", 2, 4)
        self.assertIsInstance(script, ScriptDocument)
        self.assertEqual(len(script.shots), 2)
        # 走的是旧的 dialogue 提示词（含「粗糙对话」字样）
        joined = "\n".join(str(m.get("content", "")) for m in llm.calls[0])
        self.assertIn("粗糙对话", joined)

    def test_story_source_uses_story_prompt(self) -> None:
        llm = FakeLLMClient([json.dumps(VALID_SCRIPT, ensure_ascii=False)])
        story = StoryDocument(
            title="T", synopsis="完整故事情节正文", setting="咖啡馆",
            beats=[],
        )
        script = ScriptAgent(llm).run(story, "realistic", 2, 4)
        self.assertIsInstance(script, ScriptDocument)
        joined = "\n".join(str(m.get("content", "")) for m in llm.calls[0])
        self.assertIn("完整故事情节", joined)
        self.assertIn("完整故事情节正文", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
