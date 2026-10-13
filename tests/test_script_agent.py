"""单元测试：``app.agents.script_agent.ScriptAgent`` 与容错重试。

注入 ``FakeLLMClient``（不联网），验证出参类型与三类模型输出（合法 / 带噪声 / 非法）。
运行：``.venv/Scripts/python.exe -m unittest tests.test_script_agent -v``
"""

from __future__ import annotations

import json
import unittest

from app.agents.script_agent import ScriptAgent
from app.models.schemas import ScriptDocument
from app.services.llm_client import LLMClient, LLMError
from app.utils.responses import ErrorCode

VALID_SCRIPT = {
    "title": "咖啡馆的约定",
    "style": "realistic",
    "characters": [{"name": "小明", "desc": "短发男生"}],
    "scene": "咖啡馆",
    "positions": ["1. 小明在左"],
    "shots": [
        {"shot_id": "shot_001", "dialogue": "你好", "action": "推门", "camera": "中景", "duration": 4, "prompt": "p1"},
        {"shot_id": "shot_002", "dialogue": "再见", "action": "转身", "camera": "近景", "duration": 4, "prompt": "p2"},
    ],
}


class FakeLLMClient(LLMClient):
    """按预设脚本依次返回文本的假客户端。"""

    def __init__(self, replies: list[str]) -> None:
        """初始化。

        Args:
            replies: 依次返回的文本；超出后重复最后一条。
        """
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


class TestScriptAgentHappyPath(unittest.TestCase):
    """合法 JSON → 规范化出参。"""

    def test_returns_script_document(self) -> None:
        llm = FakeLLMClient([json.dumps(VALID_SCRIPT, ensure_ascii=False)])
        script = ScriptAgent(llm).run("对话", "realistic", 2, 4)
        self.assertIsInstance(script, ScriptDocument)
        self.assertEqual(script.title, "咖啡馆的约定")
        self.assertEqual(len(script.shots), 2)
        self.assertEqual(script.shots[0].shot_id, "shot_001")
        self.assertEqual(script.characters[0].name, "小明")
        self.assertEqual(len(llm.calls), 1)

    def test_noisy_json_with_code_block(self) -> None:
        text = "好的，以下是脚本：\n```json\n" + json.dumps(VALID_SCRIPT, ensure_ascii=False) + "\n```\n请查收。"
        script = ScriptAgent(FakeLLMClient([text])).run("对话", "anime", 2, 4)
        self.assertEqual(len(script.shots), 2)

    def test_bare_list_normalized(self) -> None:
        llm = FakeLLMClient([json.dumps(VALID_SCRIPT["shots"], ensure_ascii=False)])
        script = ScriptAgent(llm).run("对话", "realistic", 2, 4)
        self.assertEqual(len(script.shots), 2)

    def test_truncates_to_shot_count(self) -> None:
        many = dict(VALID_SCRIPT)
        many["shots"] = VALID_SCRIPT["shots"] + [
            {"shot_id": "shot_003", "dialogue": "c", "duration": 4, "prompt": "p3"}
        ]
        llm = FakeLLMClient([json.dumps(many, ensure_ascii=False)])
        script = ScriptAgent(llm).run("对话", "realistic", 2, 4)
        self.assertEqual(len(script.shots), 2)

    def test_duration_clamped_to_range(self) -> None:
        weird = {
            "title": "t",
            "shots": [{"shot_id": "shot_001", "duration": 999, "prompt": "p"}],
        }
        script = ScriptAgent(FakeLLMClient([json.dumps(weird)])).run("d", "realistic", 1, 4)
        self.assertEqual(script.shots[0].duration, 10)

    def test_prompt_falls_back_to_action(self) -> None:
        item = {"title": "t", "shots": [{"shot_id": "shot_001", "action": "仅动作", "duration": 3}]}
        script = ScriptAgent(FakeLLMClient([json.dumps(item)])).run("d", "realistic", 1, 4)
        self.assertEqual(script.shots[0].prompt, "仅动作")


class TestScriptAgentRetry(unittest.TestCase):
    """非法 JSON → 自动重试 → 最终抛 LLM_BAD_JSON。"""

    def test_invalid_json_retries_and_raises(self) -> None:
        llm = FakeLLMClient(["这不是 JSON", "还是不是 JSON", "依然不是"])
        with self.assertRaises(LLMError) as ctx:
            ScriptAgent(llm).run("对话", "realistic", 2, 4)
        self.assertEqual(ctx.exception.code, ErrorCode.LLM_BAD_JSON)
        # retries 默认 2 → 共尝试 3 次
        self.assertEqual(len(llm.calls), 3)

    def test_recovers_on_second_attempt(self) -> None:
        llm = FakeLLMClient(["垃圾输出", json.dumps(VALID_SCRIPT, ensure_ascii=False)])
        script = ScriptAgent(llm).run("对话", "realistic", 2, 4)
        self.assertEqual(len(script.shots), 2)
        self.assertEqual(len(llm.calls), 2)
        # 第二次请求应追加纠错提示
        last_messages = llm.calls[-1]
        self.assertTrue(any("合法" in str(m.get("content", "")) for m in last_messages))

    def test_empty_shots_raises_bad_json(self) -> None:
        llm = FakeLLMClient([json.dumps({"title": "t", "shots": []})])
        with self.assertRaises(LLMError) as ctx:
            ScriptAgent(llm).run("对话", "realistic", 2, 4)
        self.assertEqual(ctx.exception.code, ErrorCode.LLM_BAD_JSON)


if __name__ == "__main__":
    unittest.main(verbosity=2)
