"""第三批**对抗式复核**测试：分镜确认区（awaiting_review / confirm-script）与专业剪辑区（split）。

独立于工程师既有用例（``test_confirm_script.py`` / ``test_edit_draft_api.py``），
聚焦「最容易藏坑」处并主动**尝试证伪**：

- ``review=false`` 与旧路径的阶段/日志文案**逐字一致**（R8 / AC3 / AC24）；
- ``awaiting_review`` 语义：非终态、``set_state`` 不写 ``finished_at``、``/status`` 200 且长期不推进；
- ``confirm_script`` 在**真并发连点**下只起一个 director（R10 / AC12）；
- 校验失败（duration 越界 / 空 prompt）**不脏化** ``script.json`` 与内存脚本，状态仍 ``awaiting_review``；
- 手写**旧草稿**（无 ``clip_id``）读取 + PUT 回环（R3/R4 / AC22 / AC27）；
- **split 端到端**：同 ``shot_id`` 多段导出时长 ≈ 两段之和、独立启停/排序、0 时长边界（R11/R12 / AC25 / AC26）；
- ``_normalize_clips`` 返回 ``NormalizedClip`` 的**前 4 位索引契约**（R1/R2）；
- 前端**接线真实性**（不是「只存在 id」）。

运行：``env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        .venv/Scripts/python.exe -m unittest tests.test_batch3_adversarial -v``
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
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
    ClipEdit,
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
from app.services.task_manager import NormalizedClip, TaskManager
from app.utils.paths import Paths
from app.utils.responses import ErrorCode

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
_STATIC = Paths.STATIC_DIR
_DRAFT_NAME = "edit_plan_draft.json"


# --------------------------------------------------------------------------- #
# 假智能体（避免真实 LLM / FFmpeg）
# --------------------------------------------------------------------------- #
def _story(title: str = "剧情", synopsis: str = "完整剧情正文。") -> StoryDocument:
    return StoryDocument(title=title, synopsis=synopsis, beats=[StoryBeat(index=1, summary="开端")])


def _script(title: str = "脚本", shots: int = 2) -> ScriptDocument:
    return ScriptDocument(
        title=title,
        shots=[
            Shot(shot_id=f"shot_{i:03d}", dialogue="a", action="b", camera="c",
                 duration=4, prompt=f"p{i}")
            for i in range(1, shots + 1)
        ],
    )


class _FakeStory:
    def __init__(self, *a, **k) -> None:
        pass

    def run(self, dialogue, style):
        return _story()


class _FakeScript:
    def __init__(self, *a, **k) -> None:
        pass

    def run(self, source, style, shot_count, shot_duration):
        return _script()


class _FakeDirector:
    def __init__(self, *a, **k) -> None:
        pass

    def run(self, script, config, ctx):
        out = []
        for shot in script.shots:
            result = ShotResult(shot_id=shot.shot_id, state=ShotState.SUCCESS,
                                video_path=f"shots/{shot.shot_id}.mp4", duration=4.0)
            if ctx is not None and callable(getattr(ctx, "on_shot_update", None)):
                ctx.on_shot_update(result)
            out.append(result)
        return out


class _FakeCritic:
    def __init__(self, *a, **k) -> None:
        pass

    def run(self, script, shots, skip, task_id=""):
        return EvaluationReport(task_id=task_id)


def _patch_pipeline(stack: ExitStack) -> None:
    stack.enter_context(mock.patch("app.services.task_manager.StoryAgent", _FakeStory))
    stack.enter_context(mock.patch("app.services.task_manager.ScriptAgent", _FakeScript))
    stack.enter_context(mock.patch("app.services.task_manager.DirectorAgent", _FakeDirector))
    stack.enter_context(mock.patch("app.services.task_manager.CriticAgent", _FakeCritic))
    stack.enter_context(mock.patch("app.services.task_manager.concat_videos", lambda p, o: o))
    stack.enter_context(mock.patch("app.services.task_manager.probe_duration", lambda p: 4.0))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_adv_"))
        self.mgr = TaskManager()
        self.tid = self.mgr.create_task(self.tmp, Config())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, request: PipelineRunRequest) -> None:
        with ExitStack() as stack:
            _patch_pipeline(stack)
            self.mgr.run_pipeline(self.tid, request, Config())

    def _wait_terminal(self, timeout: float = 6.0) -> TaskState:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.mgr.get(self.tid)
            if status is not None and status.state.is_terminal:
                return status.state
            time.sleep(0.02)
        return self.mgr.get(self.tid).state


# --------------------------------------------------------------------------- #
# 1. review=false 与旧路径「逐字一致」
# --------------------------------------------------------------------------- #
class TestReviewFalseByteCompat(_Base):
    def test_review_false_stage_details_verbatim(self) -> None:
        """不传 review 与显式 review=False 的阶段 detail / 状态完全一致（导演等待初始图）。"""
        self._run(PipelineRunRequest(dialogue="对话"))
        s1 = self.mgr.get(self.tid)

        other = TaskManager()
        tid2 = other.create_task(Path(tempfile.mkdtemp(prefix="sf_adv2_")), Config())
        with ExitStack() as stack:
            _patch_pipeline(stack)
            other.run_pipeline(tid2, PipelineRunRequest(dialogue="对话", review=False), Config())
        s2 = other.get(tid2)
        shutil.rmtree(other.get_output_dir(tid2), ignore_errors=True)

        for st in (s1, s2):
            self.assertEqual(st.state, TaskState.RUNNING)
            self.assertNotEqual(st.state, TaskState.AWAITING_REVIEW)

        # 阶段 detail 逐字（导演进入等待初始图，不再自动产片）
        self.assertEqual(s1.stages["story"].detail, "共 1 个情节点")
        self.assertEqual(s1.stages["script"].detail, "共 2 个镜头")
        self.assertEqual(s1.stages["director"].detail, "等待上传初始图（共 2 段）")
        self.assertEqual(
            [s1.stages[k].state for k in ("story", "script")],
            [TaskState.SUCCESS, TaskState.SUCCESS],
        )
        self.assertEqual(s1.stages["director"].state, TaskState.RUNNING)
        self.assertEqual(s1.stages["concat"].state, TaskState.PENDING)
        self.assertEqual(s1.stages["critic"].state, TaskState.PENDING)
        # 两次运行（缺省 / False）阶段 detail 相同
        self.assertEqual(
            {k: v.detail for k, v in s1.stages.items()},
            {k: v.detail for k, v in s2.stages.items()},
        )

    def test_review_false_never_enters_awaiting_review(self) -> None:
        """review=false 全程不出现 awaiting_review 字样，也没有「等待人工确认」日志。"""
        self._run(PipelineRunRequest(dialogue="对话"))
        status = self.mgr.get(self.tid)
        self.assertEqual(status.state, TaskState.RUNNING)
        self.assertEqual(status.finished_at, 0.0, "导演等待初始图阶段非终态，不得写 finished_at")
        joined = "\n".join(status.logs)
        self.assertNotIn("等待人工确认", joined)
        self.assertIn("请上传初始图开始逐段生成", joined)

    def test_review_false_status_has_full_stage_set(self) -> None:
        """review=false 的 /status 阶段集合仍为固定 5 个且 story 最前（StageName.ALL 不变）。"""
        self._run(PipelineRunRequest(dialogue="对话"))
        status = self.mgr.get(self.tid)
        self.assertEqual(list(status.stages.keys()),
                         ["story", "script", "director", "concat", "critic"])


# --------------------------------------------------------------------------- #
# 2. awaiting_review 语义
# --------------------------------------------------------------------------- #
class TestAwaitingReviewSemantics(_Base):
    def test_set_state_awaiting_does_not_write_finished_at(self) -> None:
        self.mgr.set_state(self.tid, TaskState.AWAITING_REVIEW)
        self.assertEqual(self.mgr.get(self.tid).finished_at, 0.0)

    def test_success_writes_finished_at_but_awaiting_does_not(self) -> None:
        self.mgr.set_state(self.tid, TaskState.SUCCESS)
        self.assertGreater(self.mgr.get(self.tid).finished_at, 0.0)

    def test_state_status_running_is_not_terminal_no_finished_at(self) -> None:
        """对照组：RUNNING / AWAITING_REVIEW 均非终态且都不写 finished_at。"""
        for state in (TaskState.RUNNING, TaskState.AWAITING_REVIEW):
            self.mgr.set_state(self.tid, state)
            self.assertFalse(state.is_terminal)
            self.assertEqual(self.mgr.get(self.tid).finished_at, 0.0)

    def test_review_true_long_term_no_auto_advance(self) -> None:
        """挂起后连续观察 ~0.4s：状态恒为 awaiting_review，director 仍 pending，无 shots。"""
        self._run(PipelineRunRequest(dialogue="对话", review=True))
        for _ in range(20):
            status = self.mgr.get(self.tid)
            self.assertEqual(status.state, TaskState.AWAITING_REVIEW)
            self.assertFalse(status.state.is_terminal)
            self.assertEqual(status.stages["director"].state, TaskState.PENDING)
            self.assertEqual(status.shots, [])
            self.assertEqual(status.finished_at, 0.0)
            time.sleep(0.02)

    def test_status_http_ok_while_awaiting(self) -> None:
        """HTTP /status 在 awaiting_review 时返回 200 且 ok=true、state 原样。"""
        reset_singletons()
        client = TestClient(app)
        client.__enter__()
        try:
            mgr = get_task_manager()
            tmp = Path(tempfile.mkdtemp(prefix="sf_adv_status_"))
            tid = mgr.create_task(tmp, Config())
            mgr.set_state(tid, TaskState.AWAITING_REVIEW)
            res = client.get(f"/api/pipeline/{tid}/status")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertIs(body.get("ok"), True)
            self.assertEqual(body["data"]["state"], "awaiting_review")
            self.assertEqual(body["data"]["finished_at"], 0.0)
            shutil.rmtree(tmp, ignore_errors=True)
        finally:
            client.__exit__(None, None, None)
            reset_singletons()


# --------------------------------------------------------------------------- #
# 3. confirm_script 真并发 / 幂等（R10 / AC12）
# --------------------------------------------------------------------------- #
class TestConfirmConcurrency(_Base):
    def test_concurrent_confirm_exactly_one_starts_director(self) -> None:
        """N 个线程同时 confirm：恰好 1 个成功进入 running，其余稳定 TASK_STATE_INVALID，
        且导演阶段只被初始化一次（等待初始图）。"""
        self._run(PipelineRunRequest(dialogue="对话", review=True))
        self.assertEqual(self.mgr.get(self.tid).state, TaskState.AWAITING_REVIEW)

        results: list[tuple[str, str]] = []
        start = threading.Event()
        n = 8

        def worker() -> None:
            start.wait()
            try:
                state, _rerun = self.mgr.confirm_script(self.tid, ConfirmScriptRequest())
                results.append(("ok", state))
            except KeyError as exc:
                results.append(("err", str(exc)))

        with ExitStack() as stack:
            _patch_pipeline(stack)
            threads = [threading.Thread(target=worker) for _ in range(n)]
            for t in threads:
                t.start()
            start.set()
            for t in threads:
                t.join(timeout=10)
            # 唯一成功的 resume 线程把导演阶段初始化为「等待初始图」
            deadline = time.time() + 5.0
            while time.time() < deadline and \
                    self.mgr.get(self.tid).stages["director"].state != TaskState.RUNNING:
                time.sleep(0.02)

        oks = [r for r in results if r[0] == "ok"]
        errs = [r for r in results if r[0] == "err"]
        self.assertEqual(len(oks), 1, f"应恰好 1 个成功，实得 {len(oks)}：{results}")
        self.assertEqual(len(errs), n - 1)
        for _, msg in errs:
            self.assertIn(ErrorCode.TASK_STATE_INVALID, msg)
        status = self.mgr.get(self.tid)
        self.assertEqual(status.state, TaskState.RUNNING)
        self.assertEqual(status.stages["director"].state, TaskState.RUNNING)
        self.assertEqual(status.stages["director"].detail, "等待上传初始图（共 2 段）")

    def test_second_confirm_after_success_is_rejected(self) -> None:
        """续跑到 success 后再确认 → TASK_STATE_INVALID（幂等，不起新线程）。"""
        self._run(PipelineRunRequest(dialogue="对话", review=True))
        with ExitStack() as stack:
            _patch_pipeline(stack)
            self.mgr.confirm_script(self.tid, ConfirmScriptRequest())
            self._wait_terminal()
            with self.assertRaises(KeyError) as ctx:
                self.mgr.confirm_script(self.tid, ConfirmScriptRequest())
        self.assertIn(ErrorCode.TASK_STATE_INVALID, str(ctx.exception))


# --------------------------------------------------------------------------- #
# 4. 校验失败不脏化（AC10 / AC11）
# --------------------------------------------------------------------------- #
class TestValidationNoDirtying(_Base):
    def test_invalid_duration_does_not_touch_script_json_or_record(self) -> None:
        self._run(PipelineRunRequest(dialogue="对话", review=True))
        script_json = self.tmp / "script.json"
        sentinel = script_json.read_bytes()
        before_script = self.mgr.get_script(self.tid).model_dump(mode="json")

        edited = ScriptDocument(shots=[Shot(shot_id="shot_001", duration=1, prompt="ok")])
        with self.assertRaises(KeyError) as ctx:
            self.mgr.confirm_script(self.tid, ConfirmScriptRequest(script=edited))
        self.assertIn(ErrorCode.CFG_INVALID, str(ctx.exception))

        self.assertEqual(script_json.read_bytes(), sentinel, "校验失败不得覆盖 script.json")
        self.assertEqual(self.mgr.get_script(self.tid).model_dump(mode="json"), before_script)
        self.assertEqual(self.mgr.get(self.tid).state, TaskState.AWAITING_REVIEW)

    def test_empty_prompt_does_not_touch_script_json(self) -> None:
        self._run(PipelineRunRequest(dialogue="对话", review=True))
        sentinel = (self.tmp / "script.json").read_bytes()
        edited = ScriptDocument(shots=[Shot(shot_id="shot_002", duration=4, prompt="   ")])
        with self.assertRaises(KeyError):
            self.mgr.confirm_script(self.tid, ConfirmScriptRequest(script=edited))
        self.assertEqual((self.tmp / "script.json").read_bytes(), sentinel)
        self.assertEqual(self.mgr.get(self.tid).state, TaskState.AWAITING_REVIEW)

    def test_validation_before_state_transition_no_thread_started(self) -> None:
        """校验失败时不得启动续跑线程（状态仍 waiting，director 阶段仍 pending）。"""
        self._run(PipelineRunRequest(dialogue="对话", review=True))
        with self.assertRaises(KeyError):
            self.mgr.confirm_script(
                self.tid, ConfirmScriptRequest(script=ScriptDocument(
                    shots=[Shot(shot_id="s", duration=99, prompt="x")]))
            )
        status = self.mgr.get(self.tid)
        self.assertEqual(status.state, TaskState.AWAITING_REVIEW)
        self.assertEqual(status.stages["director"].state, TaskState.PENDING)


# --------------------------------------------------------------------------- #
# 5. 旧草稿兼容（R3/R4 / AC22 / AC27）
# --------------------------------------------------------------------------- #
class _DraftBase(unittest.TestCase):
    def setUp(self) -> None:
        reset_singletons()
        self.client = TestClient(app)
        self.client.__enter__()
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_adv_draft_"))
        self.mgr = get_task_manager()
        self.tid = self.mgr.create_task(self.tmp, Config())

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        reset_singletons()


class TestLegacyDraftCompat(_DraftBase):
    def _write_legacy(self) -> list[dict]:
        legacy = [
            {"shot_id": "shot_003", "enabled": True, "start": 0.5, "end": 1.5, "order": 0},
            {"shot_id": "shot_001", "enabled": False, "start": 0.0, "end": None, "order": 1},
            {"shot_id": "shot_002", "enabled": True, "start": 0.2, "end": 0.8, "order": 2},
        ]
        (self.tmp / _DRAFT_NAME).write_text(
            json.dumps({"task_id": self.tid, "clip_count": 3, "clips": legacy}), encoding="utf-8"
        )
        return legacy

    def test_legacy_draft_read_verbatim(self) -> None:
        """手写无 clip_id 的旧草稿 → GET 原样解析（顺序/起止/启停/order 一致）。"""
        legacy = self._write_legacy()
        got = self.client.get(f"/api/pipeline/{self.tid}/edit-plan").json()["data"]["clips"]
        self.assertEqual([c["shot_id"] for c in got], ["shot_003", "shot_001", "shot_002"])
        self.assertEqual([c["enabled"] for c in got], [True, False, True])
        self.assertEqual([c["order"] for c in got], [0, 1, 2])
        self.assertAlmostEqual(got[0]["start"], 0.5, places=3)
        self.assertAlmostEqual(got[0]["end"], 1.5, places=3)
        self.assertIsNone(got[1]["end"])
        self.assertNotIn("clip_id", got[0], "读取应原样返回，不得注入 clip_id")

    def test_legacy_draft_put_roundtrip_semantically_identical(self) -> None:
        """读取旧草稿 → 原样 PUT → 再读：顺序/起止/启停/order 语义一致（AC27）。"""
        legacy = self._write_legacy()
        got = self.client.get(f"/api/pipeline/{self.tid}/edit-plan").json()["data"]["clips"]
        put = self.client.put(f"/api/pipeline/{self.tid}/edit-plan", json={"clips": got})
        self.assertIs(put.json().get("ok"), True)
        again = self.client.get(f"/api/pipeline/{self.tid}/edit-plan").json()["data"]["clips"]

        def proj(rows):
            return [
                (r["shot_id"], r["enabled"], r["start"], r["end"], r["order"])
                for r in rows
            ]

        self.assertEqual(proj(again), proj(got), "PUT 回环后语义必须一致")
        # 有效 id 回退 shot_id：未分割段等价
        for r in again:
            self.assertIn(r.get("clip_id"), (None, r["shot_id"]))

    def test_legacy_clip_edit_defaults_clip_id_none(self) -> None:
        """契约层：不带 clip_id 的请求解析后 clip_id 为 None（缺省=旧行为）。"""
        clip = ClipEdit.model_validate({"shot_id": "shot_001", "start": 0.1, "end": 0.9})
        self.assertIsNone(clip.clip_id)


# --------------------------------------------------------------------------- #
# 6. split 端到端（AC25 / AC26 / R11 / R12）
# --------------------------------------------------------------------------- #
@unittest.skipUnless(_FFMPEG_AVAILABLE, "FFmpeg 不可用，跳过 split 导出测试")
class TestSplitExportEndToEnd(unittest.TestCase):
    """真实裁剪 + 拼接，验证同 shot 多段导出正确性。"""

    def setUp(self) -> None:
        from app.utils.ffmpeg_utils import make_mock_video, probe_duration  # noqa: F401

        self.probe_duration = probe_duration
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_adv_split_"))
        (self.tmp / "shots").mkdir(parents=True, exist_ok=True)
        self.mgr = TaskManager()
        self.tid = self.mgr.create_task(self.tmp, Config())
        self.src = self.tmp / "shots" / "shot_001.mp4"
        make_mock_video(str(self.src), header="SHOT 1", duration=2, width=320, height=180)
        self.mgr._records[self.tid].script = ScriptDocument(
            title="t", shots=[Shot(shot_id="shot_001", duration=2, prompt="p")]
        )
        self.mgr.update_shot(self.tid, ShotResult(
            shot_id="shot_001", state=ShotState.SUCCESS,
            video_path="shots/shot_001.mp4", duration=2.0,
        ))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _export(self, clips: list[dict]):
        from app.models.schemas import ExportRequest  # local import

        req = ExportRequest.model_validate({"clips": clips})
        return self.mgr.export_edit_plan(self.tid, req)

    def test_split_two_segments_duration_is_sum(self) -> None:
        """AC25：两段 [0,1]+[1,2] 导出时长 ≈ 2.0s（±0.3），plan 含两段 clip_id。"""
        export_rel, plan_rel, count, total = self._export([
            {"shot_id": "shot_001", "clip_id": "shot_001#1", "enabled": True,
             "start": 0.0, "end": 1.0, "order": 0},
            {"shot_id": "shot_001", "clip_id": "shot_001#2", "enabled": True,
             "start": 1.0, "end": 2.0, "order": 1},
        ])
        self.assertEqual(count, 2)
        self.assertAlmostEqual(total, 2.0, delta=0.3)
        measured = self.probe_duration(str(self.tmp / export_rel))
        self.assertAlmostEqual(measured, 2.0, delta=0.3)
        plan = json.loads((self.tmp / plan_rel).read_text(encoding="utf-8"))
        self.assertEqual([c["clip_id"] for c in plan["clips"]], ["shot_001#1", "shot_001#2"])
        self.assertEqual([c["shot_id"] for c in plan["clips"]], ["shot_001", "shot_001"])

    def test_split_no_ghost_base_clip(self) -> None:
        """同 shot 两段都引用 → 不得再追加一个「基础 shot_001」幽灵段。"""
        _e, plan_rel, count, _t = self._export([
            {"shot_id": "shot_001", "clip_id": "shot_001#1", "enabled": True,
             "start": 0.0, "end": 0.8},
            {"shot_id": "shot_001", "clip_id": "shot_001#2", "enabled": True,
             "start": 0.8, "end": 2.0},
        ])
        self.assertEqual(count, 2)
        plan = json.loads((self.tmp / plan_rel).read_text(encoding="utf-8"))
        self.assertEqual(sum(1 for c in plan["clips"] if c["clip_id"] is None), 0,
                         "不应出现未分割的幽灵基础段")

    def test_segments_independent_enable_and_order(self) -> None:
        """AC26：把 #2 排到 #1 前并禁用 #1 → 仅导出 #2，顺序为 #2。"""
        _e, plan_rel, count, total = self._export([
            {"shot_id": "shot_001", "clip_id": "shot_001#2", "enabled": True,
             "start": 1.0, "end": 2.0, "order": 0},
            {"shot_id": "shot_001", "clip_id": "shot_001#1", "enabled": False,
             "start": 0.0, "end": 1.0, "order": 1},
        ])
        self.assertEqual(count, 1, "禁用 #1 后只应导出 #2")
        self.assertAlmostEqual(total, 1.0, delta=0.3)
        plan = json.loads((self.tmp / plan_rel).read_text(encoding="utf-8"))
        self.assertEqual([c["clip_id"] for c in plan["clips"]], ["shot_001#2"])

    def test_zero_length_segment_falls_back_to_whole(self) -> None:
        """边界（R11）：某段被裁到 0 时长 → 回退该段整段（不产生空段、不丢片）。"""
        _e, plan_rel, count, total = self._export([
            {"shot_id": "shot_001", "clip_id": "shot_001#1", "enabled": True,
             "start": 1.0, "end": 1.0},
        ])
        self.assertEqual(count, 1)
        self.assertAlmostEqual(total, 2.0, delta=0.3, msg="0 时长段应回退为整段")
        plan = json.loads((self.tmp / plan_rel).read_text(encoding="utf-8"))
        self.assertAlmostEqual(plan["clips"][0]["start"], 0.0, places=3)
        self.assertAlmostEqual(plan["clips"][0]["end"], 2.0, places=3)

    def test_all_split_segments_disabled_raises(self) -> None:
        from app.models.schemas import ExportRequest

        with self.assertRaises(KeyError) as ctx:
            self.mgr.export_edit_plan(self.tid, ExportRequest.model_validate({"clips": [
                {"shot_id": "shot_001", "clip_id": "shot_001#1", "enabled": False},
                {"shot_id": "shot_001", "clip_id": "shot_001#2", "enabled": False},
            ]}))
        self.assertIn(ErrorCode.SYS_BAD_REQUEST, str(ctx.exception))


# --------------------------------------------------------------------------- #
# 7. _normalize_clips 返回契约（R1/R2）
# --------------------------------------------------------------------------- #
class TestNormalizeReturnContract(_Base):
    def _shots(self):
        return [ShotResult(shot_id="shot_001", state=ShotState.SUCCESS,
                           video_path="shots/shot_001.mp4", duration=6.0)]

    def test_namedtuple_five_fields_first_four_verbatim(self) -> None:
        out = self.mgr._normalize_clips(
            [ClipEdit(shot_id="shot_001", start=1.0, end=3.0)], self._shots()
        )
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertIsInstance(row, tuple)
        self.assertIsInstance(row, NormalizedClip)
        self.assertEqual(len(row), 5)
        # 前 4 位按索引取值 = 旧 4 元组语义
        self.assertEqual(row[0], "shot_001")
        self.assertEqual(row[1], "shots/shot_001.mp4")
        self.assertEqual(row[2], 1.0)
        self.assertEqual(row[3], 3.0)
        self.assertIsNone(row[4])
        # 具名字段与位置一致
        self.assertEqual(row.shot_id, row[0])
        self.assertEqual(row.video_path, row[1])
        self.assertEqual(row.start, row[2])
        self.assertEqual(row.end, row[3])
        self.assertEqual(row.clip_id, row[4])

    def test_multi_split_effective_ids_all_kept(self) -> None:
        """同一 shot 的 4 段（含二次分割 #1#1 / #1#2）全部保留、有效 id 不撞。"""
        clips = [
            ClipEdit(shot_id="shot_001", clip_id="shot_001#1#1", start=0.0, end=0.5),
            ClipEdit(shot_id="shot_001", clip_id="shot_001#1#2", start=0.5, end=1.0),
            ClipEdit(shot_id="shot_001", clip_id="shot_001#2", start=1.0, end=3.0),
            ClipEdit(shot_id="shot_001", clip_id="shot_001#3", start=3.0, end=6.0),
        ]
        out = self.mgr._normalize_clips(clips, self._shots())
        self.assertEqual([r.clip_id for r in out],
                         ["shot_001#1#1", "shot_001#1#2", "shot_001#2", "shot_001#3"])
        self.assertEqual(len(out), 4)

    def test_no_clip_id_dedup_by_shot_id_backward_compatible(self) -> None:
        clips = [
            ClipEdit(shot_id="shot_001", start=0.0, end=1.0),
            ClipEdit(shot_id="shot_001", start=1.0, end=2.0),
        ]
        out = self.mgr._normalize_clips(clips, self._shots())
        self.assertEqual(len(out), 1, "无 clip_id 时同 shot 视为重复 → 保留首个（旧行为）")
        self.assertEqual((out[0][2], out[0][3]), (0.0, 1.0))


# --------------------------------------------------------------------------- #
# 8. 前端接线真实性（AC4 / AC5 / B9）
# --------------------------------------------------------------------------- #
class TestFrontendWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (_STATIC / "index.html").read_text(encoding="utf-8")
        cls.js = (_STATIC / "app.js").read_text(encoding="utf-8")

    def test_review_toggle_default_checked_and_used_in_payload(self) -> None:
        import re
        self.assertRegex(self.html, r'id="reviewToggle"[^>]*checked')
        self.assertIn('review: !!$("reviewToggle").checked', self.js,
                      "reviewToggle 必须真正参与 /run payload")

    def test_split_button_has_click_handler(self) -> None:
        self.assertIn('$("splitClipBtn").addEventListener("click", splitClip)', self.js)

    def test_confirm_buttons_wired_to_confirm_endpoint(self) -> None:
        self.assertIn('$("reviewConfirmBtn").addEventListener("click", confirmReview)', self.js)
        self.assertIn('$("reviewSkipBtn").addEventListener("click", skipReview)', self.js)
        self.assertIn('/confirm-script"', self.js)

    def test_split_appends_suffix_for_unique_ids(self) -> None:
        """split 必须给两段追加 #1/#2 后缀（多次分割永不撞号）。"""
        self.assertIn('parentId + "#1"', self.js)
        self.assertIn('parentId + "#2"', self.js)
        self.assertIn('var parentId = effectiveId(clip);', self.js)

    def test_awaiting_review_keeps_polling_and_shows_review(self) -> None:
        """轮询识别 awaiting_review：继续轮询 + 进入确认区，而不是当终态停轮询。"""
        self.assertIn('state === "awaiting_review"', self.js)
        self.assertIn("enterReviewMode", self.js)
        self.assertIn("startPolling", self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
