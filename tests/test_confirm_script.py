"""增量补充测试：分镜确认区（人审门）与原地续跑。

覆盖第三批增量（对应设计 §1.2 两条路径、§2.2 新端点、§2.5 校验）：
- ``review=false``（默认）→ ``/run`` 行为与旧版**逐字一致**（不出现 ``awaiting_review``）；
- ``review=true`` → ``script`` 落盘后挂起 ``awaiting_review``（``shots==[]``、无 ``shots/*.mp4``）；
- ``POST /confirm-script`` → 同 ``task_id`` 原地续跑至 ``success``；
- ``story`` 变更 → 重跑 ``ScriptAgent``（``rerun_script=true``）；仅改分镜 → 不重跑；
- 非待确认状态 → ``TASK_STATE_INVALID``；任务不存在 → ``TASK_NOT_FOUND``；
- ``duration`` 越界 / 空 ``prompt`` → ``CFG_INVALID``，且任务**仍停留在** ``awaiting_review``。

为避免真实 LLM / FFmpeg，本文件以 ``mock`` 替换四个智能体与拼接/探测函数；
重活（真实裁剪）由 ``tests/test_clip_normalize.py`` 覆盖。

运行：``.venv/Scripts/python.exe -m unittest tests.test_confirm_script -v``
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.deps import get_task_manager, reset_singletons
from app.main import app
from app.models.enums import ShotState, TaskState
from app.models.schemas import (
    Config,
    ConfirmScriptRequest,
    EvaluationReport,
    PipelineRunRequest,
    ScriptDocument,
    Shot,
    ShotResult,
    StoryBeat,
    StoryDocument,
)
from app.services.task_manager import TaskManager
from app.utils.responses import ErrorCode


# --------------------------------------------------------------------------- #
# 假智能体（避免真实 LLM / FFmpeg）
# --------------------------------------------------------------------------- #
def _story(title: str = "剧情", synopsis: str = "完整剧情正文。") -> StoryDocument:
    """构造一个合法剧情。"""
    return StoryDocument(title=title, synopsis=synopsis, beats=[StoryBeat(index=1, summary="开端")])


def _script(title: str = "脚本") -> ScriptDocument:
    """构造一个合法脚本（2 个镜头，时长/提示词均合规）。"""
    return ScriptDocument(
        title=title,
        shots=[
            Shot(shot_id="shot_001", dialogue="a", action="b", camera="c", duration=4, prompt="p1"),
            Shot(shot_id="shot_002", dialogue="d", action="e", camera="f", duration=4, prompt="p2"),
        ],
    )


class _FakeStory:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def run(self, dialogue, style):
        return _story()


class _FakeScript:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def run(self, source, style, shot_count, shot_duration):
        return _script()


class _FakeDirector:
    """不落盘真实视频，仅产出成功的 ShotResult（拼接/探测已被替换）。

    同时提供 ``build_prompt`` 供串行首帧链按段构建默认提示词（Mock 分支中文占位）。
    """

    def __init__(self, *args, **kwargs) -> None:
        pass

    def run(self, script, config, ctx):
        results = []
        for shot in script.shots:
            result = ShotResult(
                shot_id=shot.shot_id,
                state=ShotState.SUCCESS,
                video_path=f"shots/{shot.shot_id}.mp4",
                duration=4.0,
            )
            if ctx is not None and callable(getattr(ctx, "on_shot_update", None)):
                ctx.on_shot_update(result)
            results.append(result)
        return results

    def build_prompt(self, shot, script, index, total, config):
        return f"SEGMENT {index} / {total}\n{shot.prompt}"


class _FakeCritic:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def run(self, script, shots, skip, task_id=""):
        return EvaluationReport(task_id=task_id)


class _FakeVideoGenerator:
    """假工作流 i2v 生成器：不产真实视频，仅落盘占位文件（截尾帧已被替换）。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def generate_i2v(self, first_frame, prompt, duration, seed, size, out_path) -> str:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"DUMMY")
        return str(target)


def _fake_tail_frame(video_path, out_png) -> str:
    """假尾帧抽取：直接落盘一个占位 PNG（不依赖 FFmpeg）。"""
    target = Path(out_png)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"TAIL")
    return str(target)


def _enter_pipeline_patches(stack: ExitStack) -> None:
    """在给定 :class:`ExitStack` 中替换四个智能体、拼接/探测与链的视频生成。"""
    stack.enter_context(mock.patch("app.services.task_manager.StoryAgent", _FakeStory))
    stack.enter_context(mock.patch("app.services.task_manager.ScriptAgent", _FakeScript))
    stack.enter_context(mock.patch("app.services.task_manager.DirectorAgent", _FakeDirector))
    stack.enter_context(mock.patch("app.services.task_manager.CriticAgent", _FakeCritic))
    stack.enter_context(mock.patch("app.services.task_manager.VideoGenerator", _FakeVideoGenerator))
    stack.enter_context(
        mock.patch("app.services.first_frame_chain.extract_tail_frame", _fake_tail_frame)
    )
    stack.enter_context(
        mock.patch("app.services.task_manager.concat_videos", lambda paths, out: out)
    )
    stack.enter_context(mock.patch("app.services.task_manager.probe_duration", lambda path: 4.0))


# --------------------------------------------------------------------------- #
# 基层：临时任务目录 + 共享 TaskManager
# --------------------------------------------------------------------------- #
class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_confirm_"))
        self.mgr = TaskManager()
        self.tid = self.mgr.create_task(self.tmp, Config())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait_terminal(self, timeout: float = 5.0) -> TaskState:
        """轮询等待任务到达终态（供续跑后台线程完成）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.mgr.get(self.tid)
            if status is not None and status.state.is_terminal:
                return status.state
            time.sleep(0.02)
        return self.mgr.get(self.tid).state

    def _run_reviewed(self, request: PipelineRunRequest) -> None:
        """在打桩环境下跑 ``run_pipeline``（``review=true`` 时会在 script 后挂起）。"""
        with ExitStack() as stack:
            _enter_pipeline_patches(stack)
            self.mgr.run_pipeline(self.tid, request, Config())

    def _wait_segment(self, index: int, timeout: float = 5.0) -> dict:
        """轮询直到某段进入 ``awaiting_confirm``（后台线程异步完成）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            seg = self.mgr.get_segment(self.tid, index)
            if seg.get("state") == "awaiting_confirm":
                return seg
            time.sleep(0.02)
        return self.mgr.get_segment(self.tid, index)

    def _drive_chain(self) -> None:
        """上传初始图并逐段接受，驱动串行首帧链到全部完成（触发收尾线程）。"""
        script = self.mgr.get_script(self.tid)
        total = len(script.shots) if script else 0
        self.mgr.start_chain(self.tid, b"\x89PNG\r\n\x1a\n" + b"X" * 16)
        for index in range(1, total + 1):
            self._wait_segment(index)
            self.mgr.accept_segment(self.tid, index)


# --------------------------------------------------------------------------- #
# D1：review 分流
# --------------------------------------------------------------------------- #
class TestReviewFlagRouting(_Case):
    def test_default_review_false_runs_straight_through(self) -> None:
        """缺省 ``review``（False）→ 不经 awaiting_review，驱动串行首帧链后直达 success。"""
        with ExitStack() as stack:
            _enter_pipeline_patches(stack)
            self.mgr.run_pipeline(self.tid, PipelineRunRequest(dialogue="对话"), Config())
            self.assertNotEqual(self.mgr.get(self.tid).state, TaskState.AWAITING_REVIEW)
            self._drive_chain()
            self.assertEqual(self._wait_terminal(), TaskState.SUCCESS)
        status = self.mgr.get(self.tid)
        self.assertEqual(status.state, TaskState.SUCCESS)
        self.assertEqual(len(status.shots), 2)
        # 各阶段顺序执行且 story 最前（StageName.ALL 逐字不变）
        self.assertEqual(list(status.stages.keys()), ["story", "script", "director", "concat", "critic"])
        self.assertEqual(status.stages["director"].state, TaskState.SUCCESS)
        self.assertEqual(status.stages["critic"].state, TaskState.SUCCESS)

    def test_review_true_suspends_after_script(self) -> None:
        """``review=true`` → script 落盘后挂起 ``awaiting_review``，尚未生成任何分镜视频。"""
        self._run_reviewed(PipelineRunRequest(dialogue="对话", review=True))
        status = self.mgr.get(self.tid)
        self.assertEqual(status.state, TaskState.AWAITING_REVIEW)
        self.assertFalse(status.state.is_terminal, "awaiting_review 必须是非终态")
        self.assertEqual(status.shots, [], "挂起时不应有镜头结果")
        self.assertEqual(status.stages["story"].state, TaskState.SUCCESS)
        self.assertEqual(status.stages["script"].state, TaskState.SUCCESS)
        self.assertEqual(status.stages["director"].state, TaskState.PENDING)
        self.assertTrue((self.tmp / "script.json").exists(), "挂起前必须已落盘 script.json")
        self.assertFalse((self.tmp / "shots").exists(), "挂起时不应生成 shots/*.mp4")
        self.assertEqual(status.finished_at, 0.0, "非终态不得写 finished_at")

    def test_awaiting_review_is_not_terminal_enum(self) -> None:
        """枚举层面：``AWAITING_REVIEW`` 非终态，``SUCCESS/FAILED`` 仍为终态。"""
        self.assertFalse(TaskState.AWAITING_REVIEW.is_terminal)
        self.assertTrue(TaskState.SUCCESS.is_terminal)
        self.assertTrue(TaskState.FAILED.is_terminal)


# --------------------------------------------------------------------------- #
# D3：confirm-script 原地续跑
# --------------------------------------------------------------------------- #
class TestConfirmResume(_Case):
    def test_confirm_without_edits_resumes_to_success(self) -> None:
        """无编辑确认 → 同 ``task_id`` 续跑（先入导演等待初始图，驱动链后）至 success。"""
        self._run_reviewed(PipelineRunRequest(dialogue="对话", review=True))
        with ExitStack() as stack:
            _enter_pipeline_patches(stack)
            state, rerun = self.mgr.confirm_script(self.tid, ConfirmScriptRequest())
            self.assertEqual(state, TaskState.RUNNING.value)
            self.assertFalse(rerun)
            self.assertEqual(self.mgr.get(self.tid).stages["director"].state, TaskState.RUNNING)
            self._drive_chain()
            self.assertEqual(self._wait_terminal(), TaskState.SUCCESS)
        status = self.mgr.get(self.tid)
        self.assertEqual(len(status.shots), 2)
        self.assertEqual(status.stages["director"].state, TaskState.SUCCESS)

    def test_story_change_triggers_script_rerun(self) -> None:
        """``story`` 变更 → ``rerun_script=True`` 且覆盖落盘 story.json / script.json。"""
        self._run_reviewed(PipelineRunRequest(dialogue="对话", review=True))
        new_story = _story(title="改后的剧情", synopsis="彻底重写的完整剧情。")
        with ExitStack() as stack:
            _enter_pipeline_patches(stack)
            state, rerun = self.mgr.confirm_script(
                self.tid, ConfirmScriptRequest(story=new_story)
            )
            self.assertEqual(state, TaskState.RUNNING.value)
            self.assertTrue(rerun)
            self._drive_chain()
            self.assertEqual(self._wait_terminal(), TaskState.SUCCESS)
        payload = json.loads((self.tmp / "story.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["title"], "改后的剧情")
        self.assertEqual(self.mgr.get_story(self.tid).synopsis, "彻底重写的完整剧情。")

    def test_same_story_does_not_rerun(self) -> None:
        """``story`` 与记录相等 → 视为未变更，``rerun_script=False``。"""
        self._run_reviewed(PipelineRunRequest(dialogue="对话", review=True))
        same = self.mgr.get_story(self.tid)
        with ExitStack() as stack:
            _enter_pipeline_patches(stack)
            _, rerun = self.mgr.confirm_script(self.tid, ConfirmScriptRequest(story=same))
            self.assertFalse(rerun)
            self._drive_chain()
            self.assertEqual(self._wait_terminal(), TaskState.SUCCESS)

    def test_script_only_edit_is_adopted_without_rerun(self) -> None:
        """仅改分镜（不改剧情）→ 采用该脚本、不重跑，且落盘 script.json 为新内容。"""
        self._run_reviewed(PipelineRunRequest(dialogue="对话", review=True))
        edited = ScriptDocument(
            title="手改脚本",
            shots=[Shot(shot_id="shot_001", duration=3, prompt="edited", action="a")],
        )
        with ExitStack() as stack:
            _enter_pipeline_patches(stack)
            _, rerun = self.mgr.confirm_script(self.tid, ConfirmScriptRequest(script=edited))
            self.assertFalse(rerun)
            self._drive_chain()
            self.assertEqual(self._wait_terminal(), TaskState.SUCCESS)
        saved = json.loads((self.tmp / "script.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["title"], "手改脚本")
        self.assertEqual(len(saved["shots"]), 1)


# --------------------------------------------------------------------------- #
# 校验与错误路径
# --------------------------------------------------------------------------- #
class TestConfirmValidation(_Case):
    def test_unknown_task_raises_task_not_found(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            self.mgr.confirm_script("nope", ConfirmScriptRequest())
        self.assertIn(ErrorCode.TASK_NOT_FOUND, str(ctx.exception))

    def test_not_awaiting_state_raises_state_invalid(self) -> None:
        """非 ``awaiting_review``（此处 RUNNING）→ ``TASK_STATE_INVALID``。"""
        self.mgr.set_state(self.tid, TaskState.RUNNING)
        with self.assertRaises(KeyError) as ctx:
            self.mgr.confirm_script(self.tid, ConfirmScriptRequest())
        self.assertIn(ErrorCode.TASK_STATE_INVALID, str(ctx.exception))

    def test_pending_state_also_invalid(self) -> None:
        """刚创建（pending）同样不可确认。"""
        with self.assertRaises(KeyError) as ctx:
            self.mgr.confirm_script(self.tid, ConfirmScriptRequest())
        self.assertIn(ErrorCode.TASK_STATE_INVALID, str(ctx.exception))

    def test_duration_out_of_range_keeps_awaiting(self) -> None:
        """``duration`` 越界 → ``CFG_INVALID``，状态**仍为** ``awaiting_review``、不起续跑。"""
        self._run_reviewed(PipelineRunRequest(dialogue="对话", review=True))
        for bad in (1, 30):
            edited = ScriptDocument(
                title="t",
                shots=[Shot(shot_id="shot_001", duration=bad, prompt="ok")],
            )
            with self.assertRaises(KeyError) as ctx:
                self.mgr.confirm_script(self.tid, ConfirmScriptRequest(script=edited))
            self.assertIn(ErrorCode.CFG_INVALID, str(ctx.exception))
            self.assertIn("时长", str(ctx.exception))
            self.assertEqual(
                self.mgr.get(self.tid).state,
                TaskState.AWAITING_REVIEW,
                "校验失败后必须仍停留在 awaiting_review",
            )
            self.assertEqual(self.mgr.get(self.tid).shots, [])

    def test_empty_prompt_keeps_awaiting(self) -> None:
        """空 ``prompt`` → ``CFG_INVALID`` 且状态不变。"""
        self._run_reviewed(PipelineRunRequest(dialogue="对话", review=True))
        edited = ScriptDocument(
            title="t",
            shots=[Shot(shot_id="shot_003", duration=4, prompt="   ")],
        )
        with self.assertRaises(KeyError) as ctx:
            self.mgr.confirm_script(self.tid, ConfirmScriptRequest(script=edited))
        self.assertIn(ErrorCode.CFG_INVALID, str(ctx.exception))
        self.assertIn("分镜提示词", str(ctx.exception))
        self.assertEqual(self.mgr.get(self.tid).state, TaskState.AWAITING_REVIEW)

    def test_story_change_ignores_invalid_shot_edits(self) -> None:
        """``story`` 变更时忽略本次 shots 编辑（即使非法也不校验），只重跑脚本。"""
        self._run_reviewed(PipelineRunRequest(dialogue="对话", review=True))
        bad_script = ScriptDocument(
            title="t", shots=[Shot(shot_id="shot_001", duration=99, prompt="")]
        )
        with ExitStack() as stack:
            _enter_pipeline_patches(stack)
            _, rerun = self.mgr.confirm_script(
                self.tid,
                ConfirmScriptRequest(story=_story(title="新剧情"), script=bad_script),
            )
            self.assertTrue(rerun)
            self._drive_chain()
            self.assertEqual(self._wait_terminal(), TaskState.SUCCESS)


# --------------------------------------------------------------------------- #
# 端点层（TestClient）
# --------------------------------------------------------------------------- #
class TestConfirmEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        reset_singletons()
        self.client = TestClient(app)
        self.client.__enter__()
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_confirm_api_"))
        self.mgr = get_task_manager()
        self.tid = self.mgr.create_task(self.tmp, Config())

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        reset_singletons()

    def test_unknown_task_returns_task_not_found(self) -> None:
        res = self.client.post("/api/pipeline/deadbeefcafe/confirm-script", json={})
        body = res.json()
        self.assertIs(body.get("ok"), False)
        self.assertIn(ErrorCode.TASK_NOT_FOUND, body.get("error") or "")

    def test_not_awaiting_returns_state_invalid(self) -> None:
        res = self.client.post(f"/api/pipeline/{self.tid}/confirm-script", json={})
        body = res.json()
        self.assertIs(body.get("ok"), False)
        self.assertIn(ErrorCode.TASK_STATE_INVALID, body.get("error") or "")

    def test_success_returns_running_and_rerun_flag(self) -> None:
        # 模拟「已生成脚本并挂起」：置 awaiting_review 且注入现有脚本。
        self.mgr.set_request(self.tid, PipelineRunRequest(dialogue="对话"), Config())
        self.mgr._records[self.tid].script = _script()
        self.mgr.set_state(self.tid, TaskState.AWAITING_REVIEW)
        with ExitStack() as stack:
            _enter_pipeline_patches(stack)
            res = self.client.post(f"/api/pipeline/{self.tid}/confirm-script", json={})
            body = res.json()
            self.assertIs(body.get("ok"), True)
            self.assertEqual(body["data"]["state"], "running")
            self.assertIs(body["data"]["rerun_script"], False)
            # 等待续跑线程进入导演阶段（等待初始图），再驱动串行首帧链到成功
            deadline = time.time() + 5.0
            while time.time() < deadline and \
                    self.mgr.get(self.tid).stages["director"].state != TaskState.RUNNING:
                time.sleep(0.02)
            self.mgr.start_chain(self.tid, b"\x89PNG\r\n\x1a\n" + b"X" * 16)
            total = len(self.mgr.get_script(self.tid).shots)
            for index in range(1, total + 1):
                d2 = time.time() + 5.0
                while time.time() < d2 and \
                        self.mgr.get_segment(self.tid, index).get("state") != "awaiting_confirm":
                    time.sleep(0.02)
                self.mgr.accept_segment(self.tid, index)
            d3 = time.time() + 5.0
            while time.time() < d3 and not self.mgr.get(self.tid).state.is_terminal:
                time.sleep(0.02)
        self.assertEqual(self.mgr.get(self.tid).state, TaskState.SUCCESS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
