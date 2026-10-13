"""单元测试：``DirectorAgent.build_prompt``（串行首帧链提示词构造器）鲁棒性。

首帧降级阶梯（L1–L4）已随自建图像生成整体移除；导演层瘦身为纯函数 ``build_prompt``
+ ``_scene_card``。本文件锁定其**兜底**行为：空提示词 / 越界场景 / 缺角色卡 / 缺场景卡
均不抛异常、不中断串行首帧链。

运行：``.venv/Scripts/python.exe -m unittest tests.test_director_resilience -v``
"""

from __future__ import annotations

import unittest

from app.agents.director_agent import DirectorAgent
from app.models.enums import VideoProvider
from app.models.schemas import Character, Config, Scene, ScriptDocument, Shot


def _director() -> DirectorAgent:
    return DirectorAgent(None)


class TestDirectorBuildPromptResilience(unittest.TestCase):
    """提示词构造器在缺失/空输入下保持可用（不抛错）。"""

    def test_mock_branch_with_empty_everything_still_returns_header(self) -> None:
        script = ScriptDocument(title="t", shots=[Shot(shot_id="s1")])
        prompt = _director().build_prompt(script.shots[0], script, 1, 1, Config())
        self.assertTrue(prompt.startswith("SEGMENT 1 / 1"), prompt)

    def test_real_branch_falls_back_to_action_when_prompt_empty(self) -> None:
        script = ScriptDocument(
            title="t",
            shots=[Shot(shot_id="s1", action="she walks in", prompt="")],
        )
        cfg = Config(video_provider=VideoProvider.RUNNINGHUB, consistency_lock=False)
        self.assertEqual(_director().build_prompt(script.shots[0], script, 1, 1, cfg), "she walks in")

    def test_real_branch_empty_prompt_and_action_returns_empty(self) -> None:
        script = ScriptDocument(title="t", shots=[Shot(shot_id="s1", prompt="", action="")])
        cfg = Config(video_provider=VideoProvider.RUNNINGHUB, consistency_lock=False)
        self.assertEqual(_director().build_prompt(script.shots[0], script, 1, 1, cfg), "")

    def test_scene_card_out_of_range_falls_back_to_script_scene(self) -> None:
        script = ScriptDocument(
            title="t",
            scene="总述场景",
            scenes=[Scene(index=0, name="cafe", desc="d", lock_card="cafe card")],
            shots=[Shot(shot_id="s1", scene_index=5)],  # 越界
        )
        self.assertEqual(DirectorAgent._scene_card(script, 5), "总述场景")

    def test_scene_card_missing_lock_card_falls_back(self) -> None:
        script = ScriptDocument(
            title="t",
            scene="总述场景",
            scenes=[Scene(index=0, name="cafe", desc="d", lock_card="")],
            shots=[Shot(shot_id="s1", scene_index=0)],
        )
        self.assertEqual(DirectorAgent._scene_card(script, 0), "总述场景")

    def test_real_branch_without_characters_omits_characters_section(self) -> None:
        script = ScriptDocument(
            title="t",
            scene="",
            scenes=[Scene(index=0, name="cafe", desc="d", lock_card="cafe card")],
            shots=[Shot(shot_id="s1", prompt="she sits", scene_index=0)],
        )
        cfg = Config(video_provider=VideoProvider.RUNNINGHUB, consistency_lock=True)
        prompt = _director().build_prompt(script.shots[0], script, 1, 1, cfg)
        self.assertNotIn("CHARACTERS:", prompt)
        self.assertIn("SCENE: cafe card", prompt)

    def test_real_branch_binds_named_characters(self) -> None:
        script = ScriptDocument(
            title="t",
            scene="",
            characters=[Character(name="Alice", desc="d", lock_card="red hair")],
            scenes=[Scene(index=0, name="cafe", desc="d", lock_card="cafe card")],
            shots=[Shot(shot_id="s1", prompt="she sits", scene_index=0)],
        )
        cfg = Config(video_provider=VideoProvider.RUNNINGHUB, consistency_lock=True)
        prompt = _director().build_prompt(script.shots[0], script, 1, 1, cfg)
        self.assertIn("Alice=red hair", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
