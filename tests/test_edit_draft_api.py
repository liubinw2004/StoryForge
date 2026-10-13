"""增量补充测试：剪辑方案草稿（刷新后自动恢复）的读写与健壮性。

覆盖本轮新增能力：
- ``PUT /api/pipeline/{task_id}/edit-plan``（保存草稿）
- ``GET /api/pipeline/{task_id}/edit-plan``（读取草稿）
- ``TaskManager.save_edit_draft`` / ``get_edit_draft``

设计要点（对应本轮需求 2「刷新后自动恢复剪辑方案」）：
- 草稿落在**服务端** ``outputs/<ts>/edit_plan_draft.json``，而非浏览器，
  这样换浏览器 / 清缓存也能恢复；
- 草稿读写**绝不触发 FFmpeg**（只写 JSON），因此可随每次编辑高频调用；
- 读取失败/文件损坏必须**静默降级为空列表**，由前端退回原始剪辑，不影响主流程。

运行：``.venv/Scripts/python.exe -m unittest tests.test_edit_draft_api -v``
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.deps import get_task_manager, reset_singletons
from app.main import app
from app.models.schemas import Config, PipelineRunRequest, StoryDocument
from app.utils.responses import ErrorCode

_DRAFT_NAME = "edit_plan_draft.json"


class _DraftCase(unittest.TestCase):
    """共享：临时任务目录 + 注入全局 TaskManager 的 TestClient。"""

    def setUp(self) -> None:
        reset_singletons()
        self.client = TestClient(app)
        self.client.__enter__()
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_draft_"))
        self.mgr = get_task_manager()
        self.tid = self.mgr.create_task(self.tmp, Config())

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        reset_singletons()

    # ---------------------------------------------------------------- #
    def put(self, clips):
        return self.client.put(f"/api/pipeline/{self.tid}/edit-plan", json={"clips": clips})

    def get(self):
        return self.client.get(f"/api/pipeline/{self.tid}/edit-plan")


class TestDraftRead(_DraftCase):
    """读取路径：无草稿 / 任务不存在 / 文件损坏。"""

    def test_unknown_task_returns_task_not_found(self) -> None:
        res = self.client.get("/api/pipeline/deadbeefcafe/edit-plan")
        body = res.json()
        self.assertIs(body.get("ok"), False)
        self.assertIn(ErrorCode.TASK_NOT_FOUND, body.get("error") or "")

    def test_no_draft_returns_empty_clips(self) -> None:
        """无草稿时必须返回 ok + 空数组（而非报错），前端据此退回原始剪辑。"""
        res = self.get()
        body = res.json()
        self.assertIs(body.get("ok"), True)
        self.assertEqual(body["data"]["clips"], [])
        # 读取是只读操作，不应把草稿文件「创建」出来
        self.assertFalse((self.tmp / _DRAFT_NAME).exists())

    def test_corrupt_draft_degrades_to_empty(self) -> None:
        """草稿文件损坏 → 静默返回空列表，不得抛异常。"""
        (self.tmp / _DRAFT_NAME).write_text("{ 这不是合法 JSON", encoding="utf-8")
        res = self.get()
        body = res.json()
        self.assertIs(body.get("ok"), True)
        self.assertEqual(body["data"]["clips"], [])

    def test_draft_with_wrong_shape_degrades_to_empty(self) -> None:
        """clips 不是数组（结构异常）→ 同样降级为空列表。"""
        (self.tmp / _DRAFT_NAME).write_text(
            json.dumps({"clips": "oops"}), encoding="utf-8"
        )
        body = self.get().json()
        self.assertIs(body.get("ok"), True)
        self.assertEqual(body["data"]["clips"], [])

    def test_legacy_draft_without_clip_id_reads_back(self) -> None:
        """旧草稿（无 ``clip_id`` 键）必须原样读回，保证向后兼容（AC22/AC27）。"""
        legacy = {
            "task_id": self.tid,
            "clip_count": 2,
            "clips": [
                {"shot_id": "shot_001", "enabled": True, "start": 0.0, "end": 1.0, "order": 0},
                {"shot_id": "shot_002", "enabled": False, "start": 0.0, "end": None, "order": 1},
            ],
        }
        (self.tmp / _DRAFT_NAME).write_text(json.dumps(legacy), encoding="utf-8")
        body = self.get().json()
        self.assertIs(body.get("ok"), True)
        got = body["data"]["clips"]
        self.assertEqual([c["shot_id"] for c in got], ["shot_001", "shot_002"])
        self.assertNotIn("clip_id", got[0], "旧草稿无 clip_id 键也应照常可读")
        self.assertIsNone(got[1]["end"])


class TestDraftWriteReadRoundTrip(_DraftCase):
    """写入 → 读取 的完整回环，重点验证顺序与裁剪参数不丢。"""

    def test_unknown_task_cannot_save(self) -> None:
        res = self.client.put(
            "/api/pipeline/deadbeefcafe/edit-plan", json={"clips": []}
        )
        body = res.json()
        self.assertIs(body.get("ok"), False)
        self.assertIn(ErrorCode.TASK_NOT_FOUND, body.get("error") or "")

    def test_round_trip_preserves_order_enabled_and_trim(self) -> None:
        clips = [
            {"shot_id": "shot_003", "enabled": True, "start": 0.5, "end": 1.5, "order": 0},
            {"shot_id": "shot_001", "enabled": False, "start": 0.0, "end": None, "order": 1},
            {"shot_id": "shot_002", "enabled": True, "start": 0.2, "end": 0.8, "order": 2},
        ]
        res = self.put(clips)
        body = res.json()
        self.assertIs(body.get("ok"), True)
        self.assertEqual(body["data"]["clip_count"], 3)

        got = self.get().json()["data"]["clips"]
        self.assertEqual([c["shot_id"] for c in got], ["shot_003", "shot_001", "shot_002"])
        self.assertEqual([c["enabled"] for c in got], [True, False, True])
        self.assertAlmostEqual(got[0]["start"], 0.5, places=3)
        self.assertAlmostEqual(got[0]["end"], 1.5, places=3)
        self.assertIsNone(got[1]["end"], "end=None 必须原样保留（语义：到片段结尾）")
        self.assertEqual([c["order"] for c in got], [0, 1, 2])

    def test_empty_clips_is_valid_and_overwrites_previous(self) -> None:
        """空方案是合法写入（用于「重置后清空草稿」），且必须覆盖旧草稿。"""
        self.assertEqual(self.put([{"shot_id": "shot_001"}]).json()["data"]["clip_count"], 1)
        self.assertEqual(self.put([]).json()["data"]["clip_count"], 0)
        self.assertEqual(self.get().json()["data"]["clips"], [])

    def test_repeated_saves_keep_only_latest(self) -> None:
        """高频保存（防抖兜底）语义：后写覆盖先写，不累积。"""
        self.put([{"shot_id": "shot_001", "order": 0}, {"shot_id": "shot_002", "order": 1}])
        self.put([{"shot_id": "shot_002", "order": 0}, {"shot_id": "shot_001", "order": 1}])
        got = self.get().json()["data"]["clips"]
        self.assertEqual([c["shot_id"] for c in got], ["shot_002", "shot_001"])
        self.assertEqual(len(got), 2)

    def test_draft_round_trip_preserves_clip_id_for_split(self) -> None:
        """split 草稿：同 ``shot_id`` 的两段（``clip_id`` 互异）回环后仍为两段。"""
        clips = [
            {"shot_id": "shot_001", "clip_id": "shot_001#1", "enabled": True,
             "start": 0.0, "end": 1.0, "order": 0},
            {"shot_id": "shot_001", "clip_id": "shot_001#2", "enabled": True,
             "start": 1.0, "end": 2.0, "order": 1},
        ]
        self.assertEqual(self.put(clips).json()["data"]["clip_count"], 2)
        got = self.get().json()["data"]["clips"]
        self.assertEqual([c["clip_id"] for c in got], ["shot_001#1", "shot_001#2"])
        self.assertEqual([c["shot_id"] for c in got], ["shot_001", "shot_001"])

    def test_draft_with_null_clip_id_is_preserved(self) -> None:
        """未分割片段 ``clip_id`` 传 null → 回环后仍为 null（等价旧行为）。"""
        self.put([{"shot_id": "shot_001", "clip_id": None, "enabled": True,
                   "start": 0.0, "end": None, "order": 0}])
        got = self.get().json()["data"]["clips"]
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0]["clip_id"])


class TestDraftIsolationAndSafety(_DraftCase):
    """草稿落盘位置安全、且不产生任何 FFmpeg 副作用。"""

    def test_draft_file_lives_inside_task_dir(self) -> None:
        self.put([{"shot_id": "shot_001"}])
        draft = self.tmp / _DRAFT_NAME
        self.assertTrue(draft.is_file(), "草稿未落盘")
        # 必须严格位于任务目录内，且不得借由 shot_id 影响路径
        self.assertEqual(draft.resolve().parent, self.tmp.resolve())

    def test_draft_does_not_trigger_ffmpeg_or_create_exports(self) -> None:
        """保存草稿只写 JSON：不得创建 exports/ 目录、不得生成成片。"""
        self.put([{"shot_id": "shot_001", "start": 0.5, "end": 1.0}])
        self.assertFalse(
            (self.tmp / "exports").exists(), "保存草稿不应触发导出（不应创建 exports/）"
        )
        self.assertFalse((self.tmp / "final_video.mp4").exists())

    def test_draft_does_not_touch_final_video(self) -> None:
        """回归：保存草稿不得改动已存在的 final_video.mp4。"""
        final = self.tmp / "final_video.mp4"
        final.write_bytes(b"ORIGINAL-BYTES")
        before = final.read_bytes()
        self.put([{"shot_id": "shot_001", "enabled": False}])
        self.assertEqual(final.read_bytes(), before)

    def test_manager_api_returns_clip_count(self) -> None:
        from app.models.schemas import ClipEdit, ExportRequest

        count = self.mgr.save_edit_draft(
            self.tid,
            ExportRequest(clips=[ClipEdit(shot_id="shot_001"), ClipEdit(shot_id="shot_002")]),
        )
        self.assertEqual(count, 2)
        self.assertEqual(len(self.mgr.get_edit_draft(self.tid)), 2)

    def test_manager_get_draft_on_unknown_task_is_empty(self) -> None:
        """``get_edit_draft`` 对未知任务必须静默返回 []（不是抛异常）。"""
        self.assertEqual(self.mgr.get_edit_draft("nosuchtask"), [])


class TestStoryRerunPayloadContract(unittest.TestCase):
    """需求 1「编辑剧情 → 重新生成分镜」的前端请求体契约。

    前端复用 ``POST /api/pipeline/run`` 并携带 ``story``（跳过剧情补全），
    这里只校验请求模型的接受与回环，不触发真实流水线。
    """

    def test_accepts_external_story_and_defaults(self) -> None:
        story = StoryDocument(
            title="改后的标题",
            logline="一句话",
            synopsis="我改写后的完整剧情。",
            setting="深夜便利店",
            genre="悬疑",
        )
        req = PipelineRunRequest(
            dialogue="原始对话（仅溯源）",
            style="realistic",
            shot_count=3,
            shot_duration=2,
            story=story,
            skip_story=False,
        )
        self.assertIsNotNone(req.story)
        self.assertFalse(req.skip_story)
        self.assertEqual(req.story.synopsis, "我改写后的完整剧情。")

        dumped = req.model_dump(mode="json")
        self.assertEqual(dumped["story"]["title"], "改后的标题")
        # 回环：前端发出去的 JSON 能被后端重新解析
        again = PipelineRunRequest.model_validate(dumped)
        self.assertEqual(again.story.synopsis, "我改写后的完整剧情。")

    def test_story_fields_are_optional_with_defaults(self) -> None:
        """story / skip_story 不传时必须保持向后兼容（老前端仍可用）。"""
        req = PipelineRunRequest(dialogue="对话")
        self.assertIsNone(req.story)
        self.assertFalse(req.skip_story)
        self.assertEqual(req.shot_count, 4)

    def test_frontend_edited_story_shape_is_accepted(self) -> None:
        """模拟前端 ``buildStoryFromForm()`` 产出的完整对象（含只读字段透传）。"""
        payload = {
            "dialogue": "对话",
            "story": {
                "title": "T",
                "logline": "",
                "synopsis": "S",
                "setting": "",
                "characters": [{"name": "小明", "desc": "短发"}],
                "beats": [{"index": 1, "summary": "开端", "purpose": "铺垫", "location": "家"}],
                "genre": "都市",
                "theme": "",
                "source_dialogue": "原始",
                "extra": {},
            },
        }
        req = PipelineRunRequest.model_validate(payload)
        self.assertEqual(req.story.characters[0].name, "小明")
        self.assertEqual(req.story.beats[0].purpose, "铺垫")
        self.assertEqual(req.story.source_dialogue, "原始")


if __name__ == "__main__":
    unittest.main(verbosity=2)
