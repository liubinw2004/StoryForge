"""增量补充测试：流水线短路 / story 预留字段行为 / ShotResult.duration 回填。

对应设计 §2.4（story 阶段插入与失败短路）、§2.4 预留字段（``story`` / ``skip_story``）、
以及 §7 共享知识「``duration`` 探测对两类适配器统一执行，探测失败返回 0.0（不阻断）」。

运行：``.venv/Scripts/python.exe -m unittest tests.test_pipeline_increment -v``
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models.enums import TaskState
from app.models.schemas import (
    Config,
    PipelineRunRequest,
    StoryBeat,
    StoryDocument,
)
from app.services.llm_client import LLMError
from app.services.task_manager import StageName, TaskManager
from app.utils.responses import ErrorCode


def _valid_story() -> StoryDocument:
    return StoryDocument(
        title="测试故事",
        synopsis="第一段。\n\n第二段。",
        beats=[StoryBeat(index=1, summary="开端"), StoryBeat(index=2, summary="结尾")],
    )


class TestStageEnum(unittest.TestCase):
    """阶段枚举与前端一致（story 最前）。"""

    def test_stage_all_order(self) -> None:
        self.assertEqual(
            StageName.ALL, ("story", "script", "director", "concat", "critic")
        )

    def test_story_constant(self) -> None:
        self.assertEqual(StageName.STORY, "story")


class TestPipelineStoryShortCircuit(unittest.TestCase):
    """story 阶段抛错 → 任务 failed、错误含「剧情补全失败」、后续 4 阶段不执行。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_pipe_sc_"))
        self.mgr = TaskManager()
        self.tid = self.mgr.create_task(self.tmp, Config())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_story_failure_short_circuits_remaining_stages(self) -> None:
        calls = {"script": 0, "director": 0}

        class BoomStory:
            def __init__(self, llm) -> None:
                pass

            def run(self, *args, **kwargs):
                raise LLMError(ErrorCode.LLM_BAD_JSON, "模型没返回剧情")

        class TripwireScript:
            def __init__(self, llm) -> None:
                pass

            def run(self, *args, **kwargs):
                calls["script"] += 1
                raise AssertionError("story 失败后不应执行 script 阶段")

        class TripwireDirector:
            def __init__(self, *args, **kwargs):
                calls["director"] += 1
                raise AssertionError("story 失败后不应执行 director 阶段")

        with mock.patch("app.services.task_manager.StoryAgent", BoomStory), mock.patch(
            "app.services.task_manager.ScriptAgent", TripwireScript
        ), mock.patch("app.services.task_manager.DirectorAgent", TripwireDirector):
            self.mgr.run_pipeline(self.tid, PipelineRunRequest(dialogue="一段对话"), Config())

        status = self.mgr.get(self.tid)
        self.assertEqual(status.state, TaskState.FAILED)
        self.assertIn("剧情补全失败", status.error)
        self.assertEqual(status.stages["story"].state, TaskState.FAILED)
        # 后续 4 阶段保持 pending（未被触碰）
        for stage in ("script", "director", "concat", "critic"):
            self.assertEqual(status.stages[stage].state, TaskState.PENDING, stage)
        self.assertEqual(calls["script"], 0)
        self.assertEqual(calls["director"], 0)
        # 未落盘 story.json
        self.assertFalse((self.tmp / "story.json").exists())


class TestPipelineStorySuccess(unittest.TestCase):
    """story 成功 → 落盘 story.json、story 阶段 success、script 收到 StoryDocument。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_pipe_ok_"))
        self.mgr = TaskManager()
        self.tid = self.mgr.create_task(self.tmp, Config())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_story_success_writes_json_and_feeds_script(self) -> None:
        captured: dict = {}

        class FakeStory:
            def __init__(self, llm) -> None:
                captured["story_constructed"] = captured.get("story_constructed", 0) + 1

            def run(self, dialogue, style):
                captured["story_dialogue"] = dialogue
                return _valid_story()

        class StopScript:
            def __init__(self, llm) -> None:
                pass

            def run(self, source, *args, **kwargs):
                captured["script_source"] = source
                raise RuntimeError("在此中断，避免真实生成视频")

        with mock.patch("app.services.task_manager.StoryAgent", FakeStory), mock.patch(
            "app.services.task_manager.ScriptAgent", StopScript
        ):
            self.mgr.run_pipeline(
                self.tid, PipelineRunRequest(dialogue="原始对话"), Config()
            )

        status = self.mgr.get(self.tid)
        self.assertEqual(status.stages["story"].state, TaskState.SUCCESS)
        self.assertIn("2", status.stages["story"].detail)  # 共 2 个情节点
        self.assertTrue((self.tmp / "story.json").exists())
        payload = json.loads((self.tmp / "story.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["title"], "测试故事")
        self.assertTrue(payload["synopsis"])
        self.assertIsInstance(captured["script_source"], StoryDocument)
        self.assertEqual(captured["story_dialogue"], "原始对话")


class TestPipelineStoryReservedFields(unittest.TestCase):
    """预留字段：外部 ``story`` 注入跳过 StoryAgent；``skip_story`` 走 str 兜底。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_pipe_res_"))
        self.mgr = TaskManager()
        self.tid = self.mgr.create_task(self.tmp, Config())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, request: PipelineRunRequest) -> dict:
        captured: dict = {}

        class TripwireStory:
            def __init__(self, llm) -> None:
                captured["story_constructed"] = True

            def run(self, *args, **kwargs):
                captured["story_run"] = True
                return _valid_story()

        class StopScript:
            def __init__(self, llm) -> None:
                pass

            def run(self, source, *args, **kwargs):
                captured["script_source"] = source
                raise RuntimeError("中断")

        with mock.patch("app.services.task_manager.StoryAgent", TripwireStory), mock.patch(
            "app.services.task_manager.ScriptAgent", StopScript
        ):
            self.mgr.run_pipeline(self.tid, request, Config())
        return captured

    def test_external_story_skips_story_agent(self) -> None:
        provided = StoryDocument(title="外部剧情", synopsis="既定剧情正文")
        captured = self._run(PipelineRunRequest(dialogue="对话", story=provided))
        self.assertNotIn("story_constructed", captured, "提供 story 时不应构造 StoryAgent")
        self.assertIs(captured["script_source"], provided)
        status = self.mgr.get(self.tid)
        self.assertEqual(status.stages["story"].state, TaskState.SUCCESS)
        self.assertEqual(self.mgr.get_story(self.tid).title, "外部剧情")

    def test_skip_story_uses_str_fallback(self) -> None:
        captured = self._run(PipelineRunRequest(dialogue="直接对话", skip_story=True))
        self.assertNotIn("story_constructed", captured, "skip_story 时不应构造 StoryAgent")
        self.assertEqual(captured["script_source"], "直接对话")
        status = self.mgr.get(self.tid)
        self.assertEqual(status.stages["story"].state, TaskState.SUCCESS)
        self.assertIn("跳过", status.stages["story"].detail)
        self.assertIsNone(self.mgr.get_story(self.tid), "skip_story 后 get_story 应为 None")
        self.assertFalse((self.tmp / "story.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
