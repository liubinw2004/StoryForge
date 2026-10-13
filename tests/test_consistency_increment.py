"""人物/场景一致性增量测试：提示词锁定卡 + 串行首帧链提示词构造。

覆盖：
1. ``build_video_prompt`` 拼装顺序、段落省略、单卡截断、**超长裁剪（SHOT 永不截断）**；
2. ``ConfigStore._coerce_bool`` / ``_sanitize_extended_fields``：宽松布尔解析矩阵、
   ``consistency_lock``（唯一保留开关）回落默认，``rh_*`` 成本安全校验，其余字段逐字保留；
3. ``ScriptAgent._normalize`` 解析 scenes / lock_card / scene_index（越界 clamp）；
4. 导演层 ``build_prompt``：Mock 中文占位分支 / 真实锁定卡拼装分支（假 generator 不需要）。

运行（**禁代理**）::

    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        .venv/Scripts/python.exe -m unittest tests.test_consistency_increment -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.agents.director_agent import DirectorAgent
from app.agents.script_agent import ScriptAgent
from app.models.enums import VideoProvider
from app.models.schemas import Character, Config, Scene, ScriptDocument, Shot
from app.services import config_store as config_store_mod
from app.services.config_store import ConfigStore
from app.services.prompt_templates import (
    LOCK_CARD_MAX_CHARS,
    VIDEO_STYLE_PREFIX,
    build_video_prompt,
)


# --------------------------------------------------------------------------- #
# 1) build_video_prompt
# --------------------------------------------------------------------------- #
class TestBuildVideoPrompt(unittest.TestCase):
    """提示词拼装与裁剪规则。"""

    def test_segment_order(self) -> None:
        out = build_video_prompt(
            "a cat jumps", "realistic", [("Alice", "red hair"), ("Bob", "tall")], "cafe night"
        )
        self.assertTrue(out.startswith(VIDEO_STYLE_PREFIX), out)
        self.assertIn("realistic style", out)
        i_scene = out.index("SCENE: ")
        i_chars = out.index("CHARACTERS: ")
        i_shot = out.index("SHOT: ")
        self.assertLess(i_scene, i_chars, "SCENE 必须在 CHARACTERS 之前")
        self.assertLess(i_chars, i_shot, "CHARACTERS 必须在 SHOT 之前")
        self.assertTrue(out.endswith("SHOT: a cat jumps"))
        self.assertIn("Alice=red hair", out)
        self.assertIn("Bob=tall", out)

    def test_omit_missing_segments(self) -> None:
        out = build_video_prompt("body", "anime", [], "")
        self.assertNotIn("SCENE:", out)
        self.assertNotIn("CHARACTERS:", out)
        self.assertIn("SHOT: body", out)

    def test_plain_string_cards(self) -> None:
        out = build_video_prompt("body", "realistic", ["a alone"], "")
        self.assertIn("CHARACTERS: a alone", out)

    def test_each_card_truncated(self) -> None:
        long_card = "x" * (LOCK_CARD_MAX_CHARS + 120)
        out = build_video_prompt("body", "realistic", [long_card], "")
        segment = out.split("CHARACTERS: ", 1)[1].split(" | SHOT:", 1)[0]
        self.assertLessEqual(len(segment), LOCK_CARD_MAX_CHARS)

    def test_shot_never_truncated(self) -> None:
        shot = "SHOT_BODY_" + "z" * 300
        cards = ["card%d-%s" % (i, "c" * 300) for i in range(6)]
        scene = "s" * 800
        out = build_video_prompt(shot, "realistic", cards, scene, budget=200)
        self.assertIn(shot, out, "SHOT 主体不得被截断")

    def test_drop_trailing_cards_first(self) -> None:
        """预算不足时先丢**尾部**角色卡：应保留首张、丢弃后两张。"""
        first = "A" * 120
        second = "B" * 120
        third = "C" * 120
        out = build_video_prompt("s", "realistic", [first, second, third], "", budget=250)
        self.assertIn(first, out)
        self.assertNotIn(second, out)
        self.assertNotIn(third, out)

    def test_never_exceeds_when_shot_fits(self) -> None:
        out = build_video_prompt("body", "realistic", ["x" * 300], "y" * 400, budget=250)
        self.assertLessEqual(len(out), 250)
        self.assertIn("SHOT: body", out)


# --------------------------------------------------------------------------- #
# 2) ConfigStore._sanitize_extended_fields
# --------------------------------------------------------------------------- #
class TestSanitizeExtendedFields(unittest.TestCase):
    sanitize = staticmethod(config_store_mod.ConfigStore._sanitize_extended_fields)

    def test_valid_values_untouched(self) -> None:
        data = {
            "consistency_lock": False,
            "llm_model": "m",
            "rh_workflow_id": "wf-1",
            "rh_instance_type": "plus",
            "rh_retain_seconds": 60,
        }
        self.assertEqual(self.sanitize(dict(data)), data)

    def test_lenient_bools_are_coerced_and_written_back(self) -> None:
        """宽松表示（数字 / 字符串）→ 写回**真正的 bool**，用户的「关」必须真能关掉。"""
        out = self.sanitize(
            {
                "consistency_lock": 1,
                "llm_model": "deepseek-chat",
            }
        )
        self.assertIs(out["consistency_lock"], True)
        self.assertEqual(out["llm_model"], "deepseek-chat")

    def test_unparseable_bool_is_removed_to_default(self) -> None:
        """无法解析 → 记 warn；``consistency_lock`` 非付费开关，删键回落默认（True）。"""
        data = {
            "consistency_lock": [],          # 列表：无法解析
            "llm_model": "deepseek-chat",
            "shot_count": 6,
        }
        out = self.sanitize(dict(data))
        self.assertNotIn("consistency_lock", out, "非付费开关：删键回落默认")
        # 反向断言：其它字段逐字保留
        self.assertEqual(out["llm_model"], "deepseek-chat")
        self.assertEqual(out["shot_count"], 6)
        self.assertNotEqual(out["shot_count"], 4)

    def test_unparseable_string_value_is_removed(self) -> None:
        """无法识别的字符串同样走「无法解析」分支。"""
        out = self.sanitize({"consistency_lock": "bad", "style": "anime"})
        self.assertNotIn("consistency_lock", out)
        self.assertEqual(out["style"], "anime")

    def test_missing_keys_are_noop(self) -> None:
        data = {"llm_model": "m", "resolution": "1080p"}
        self.assertEqual(self.sanitize(dict(data)), data)

    def test_does_not_touch_other_keys(self) -> None:
        data = {"consistency_lock": [], "video_provider": "mock", "extra": {"a": 1}}
        out = self.sanitize(dict(data))
        self.assertEqual(out["video_provider"], "mock")
        self.assertEqual(out["extra"], {"a": 1})
        self.assertNotIn("consistency_lock", out)

    def test_rh_instance_type_invalid_rewritten_to_default(self) -> None:
        out = self.sanitize({"rh_instance_type": "xxx", "llm_model": "m"})
        self.assertEqual(out["rh_instance_type"], "default")
        self.assertEqual(out["llm_model"], "m")

    def test_rh_retain_seconds_clamped(self) -> None:
        self.assertEqual(self.sanitize({"rh_retain_seconds": 5})["rh_retain_seconds"], 10)
        self.assertEqual(self.sanitize({"rh_retain_seconds": 999})["rh_retain_seconds"], 180)

    def test_rh_string_fields_invalid_removed(self) -> None:
        out = self.sanitize({"rh_workflow_id": "", "rh_first_frame_node_id": 123, "llm_model": "m"})
        self.assertNotIn("rh_workflow_id", out)
        self.assertNotIn("rh_first_frame_node_id", out)
        self.assertEqual(out["llm_model"], "m")

    def test_integration_load_keeps_others(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sf_sanitize_") as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "llm_mode": "openai",
                        "llm_model": "deepseek-chat",
                        "shot_count": 7,
                        "consistency_lock": "bad",
                        "rh_instance_type": "junk",
                        "rh_retain_seconds": 9999,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            cfg = ConfigStore(path).get_raw()
            # 非法新字段 → 回落默认
            self.assertTrue(cfg.consistency_lock)
            self.assertEqual(cfg.rh_instance_type, "default")
            self.assertEqual(cfg.rh_retain_seconds, 180)
            # 其它字段逐字保留（未被整份重置）
            self.assertEqual(cfg.llm_model, "deepseek-chat")
            self.assertEqual(cfg.shot_count, 7)
            self.assertEqual(cfg.llm_mode.value, "openai")


# --------------------------------------------------------------------------- #
# 3) ConfigStore._coerce_bool（宽松布尔解析矩阵）
# --------------------------------------------------------------------------- #
class TestCoerceBool(unittest.TestCase):
    """``_coerce_bool`` 取值矩阵：可解析 → bool；不可解析 → None。"""

    coerce = staticmethod(config_store_mod.ConfigStore._coerce_bool)

    def test_truthy_literals(self) -> None:
        for value in (True, 1, 2, -1, 1.5, "true", "TRUE", " True ", "1", "yes", "y", "on"):
            with self.subTest(value=value):
                self.assertIs(self.coerce(value), True)

    def test_falsy_literals(self) -> None:
        for value in (False, 0, 0.0, "false", "FALSE", " false ", "0", "no", "n", "off", ""):
            with self.subTest(value=value):
                self.assertIs(self.coerce(value), False)

    def test_unparseable_returns_none(self) -> None:
        for value in (None, [], {}, (), "maybe", "2", "是"):
            with self.subTest(value=value):
                self.assertIsNone(self.coerce(value), f"{value!r} 不应被解析为布尔值")

    def test_bool_is_not_confused_with_int(self) -> None:
        """``bool`` 是 ``int`` 子类，必须优先按 ``bool`` 原样返回。"""
        self.assertIs(self.coerce(True), True)
        self.assertIs(self.coerce(False), False)


# --------------------------------------------------------------------------- #
# 4) ScriptAgent._normalize（scenes / lock_card / scene_index）
# --------------------------------------------------------------------------- #
class TestScriptAgentScenes(unittest.TestCase):
    @staticmethod
    def _normalize(data: dict) -> ScriptDocument:
        return ScriptAgent(None)._normalize(data, "realistic", 4, 4)

    def test_scenes_lock_cards_and_scene_index(self) -> None:
        data = {
            "characters": [{"name": "A", "desc": "d", "lock_card": "red hair"}],
            "scenes": [
                {"index": 5, "name": "cafe", "desc": "d", "lock_card": "neon"},
                {"index": 9, "name": "street", "desc": "d", "lock_card": "rain"},
            ],
            "shots": [
                {"shot_id": "shot_001", "scene_index": 0, "duration": 4, "prompt": "p1"},
                {"shot_id": "shot_002", "scene_index": 1, "duration": 4, "prompt": "p2"},
                {"shot_id": "shot_003", "scene_index": 99, "duration": 4, "prompt": "p3"},
            ],
        }
        script = self._normalize(data)
        self.assertEqual([s.name for s in script.scenes], ["cafe", "street"])
        self.assertEqual([s.index for s in script.scenes], [0, 1], "index 应按数组顺序重排")
        self.assertEqual(script.characters[0].lock_card, "red hair")
        # scene_index 99 越界 → clamp 到 max(0, len(scenes)-1) = 1
        self.assertEqual([s.scene_index for s in script.shots], [0, 1, 1])

    def test_missing_scene_index_defaults_zero(self) -> None:
        script = self._normalize({"shots": [{"shot_id": "s1", "duration": 4, "prompt": "p"}]})
        self.assertEqual(script.shots[0].scene_index, 0)
        self.assertEqual(script.scenes, [])

    def test_invalid_scene_index_string_and_negative(self) -> None:
        data = {
            "scenes": [{"name": "a"}, {"name": "b"}],
            "shots": [
                {"shot_id": "s1", "scene_index": "xx", "duration": 4, "prompt": "p"},
                {"shot_id": "s2", "scene_index": -3, "duration": 4, "prompt": "p"},
            ],
        }
        script = self._normalize(data)
        self.assertEqual([s.scene_index for s in script.shots], [0, 0])


# --------------------------------------------------------------------------- #
# 5) DirectorAgent.build_prompt（串行首帧链的提示词构造器）
# --------------------------------------------------------------------------- #
class TestDirectorBuildPrompt(unittest.TestCase):
    """导演层瘦身后只保留 ``build_prompt``（Mock 中文占位 / 真实锁定卡拼装）。"""

    @staticmethod
    def _script() -> ScriptDocument:
        return ScriptDocument(
            title="t",
            scene="咖啡馆",
            characters=[Character(name="Alice", desc="d", lock_card="red hair")],
            scenes=[Scene(index=0, name="cafe", desc="d", lock_card="cafe card")],
            shots=[
                Shot(shot_id="shot_001", dialogue="你好", action="推门而入",
                     duration=2, prompt="she pushes the door", scene_index=0),
            ],
        )

    def test_mock_branch_returns_chinese_placeholder(self) -> None:
        director = DirectorAgent(None)
        prompt = director.build_prompt(
            self._script().shots[0], self._script(), 1, 3, Config()
        )
        self.assertTrue(prompt.startswith("SEGMENT 1 / 3"), prompt)
        self.assertIn("咖啡馆", prompt)

    def test_real_branch_with_consistency_lock_binds_character_and_scene(self) -> None:
        director = DirectorAgent(None)
        cfg = Config(video_provider=VideoProvider.RUNNINGHUB, consistency_lock=True)
        prompt = director.build_prompt(
            self._script().shots[0], self._script(), 1, 3, cfg
        )
        self.assertIn("Alice=red hair", prompt)
        self.assertIn("SCENE: cafe card", prompt)
        self.assertIn("SHOT: she pushes the door", prompt)

    def test_real_branch_without_lock_uses_raw_prompt(self) -> None:
        director = DirectorAgent(None)
        cfg = Config(video_provider=VideoProvider.RUNNINGHUB, consistency_lock=False)
        prompt = director.build_prompt(
            self._script().shots[0], self._script(), 1, 3, cfg
        )
        self.assertEqual(prompt, "she pushes the door")


if __name__ == "__main__":
    unittest.main(verbosity=2)
