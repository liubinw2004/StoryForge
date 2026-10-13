"""增量补充测试：剪辑 EDL 归一化（``TaskManager._normalize_clips``）与导出。

覆盖设计文档 §3.1 的**全部边界规则**，以及导出不污染 ``final_video.mp4``（AC9）。

运行：``.venv/Scripts/python.exe -m unittest tests.test_clip_normalize -v``
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from app.models.enums import ShotState
from app.models.schemas import (
    ClipEdit,
    Config,
    ExportRequest,
    ScriptDocument,
    Shot,
    ShotResult,
)
from app.services.task_manager import TaskManager
from app.utils.ffmpeg_utils import concat_videos, make_mock_video, probe_duration
from app.utils.responses import ErrorCode

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None


def _shot(
    shot_id: str,
    video_path: str = "shots/x.mp4",
    duration: float = 2.0,
    state: ShotState = ShotState.SUCCESS,
) -> ShotResult:
    """构造一个镜头结果（便于纯函数测试）。"""
    return ShotResult(
        shot_id=shot_id, state=state, video_path=video_path, duration=duration
    )


def _clips(pairs: list[tuple[str, float, float | None]]) -> list[ClipEdit]:
    """把 ``(shot_id, start, end)`` 列表转为 ``ClipEdit`` 列表（默认启用）。"""
    return [
        ClipEdit(shot_id=sid, enabled=True, start=start, end=end)
        for sid, start, end in pairs
    ]


class TestNormalizeClipsBoundaries(unittest.TestCase):
    """设计 §3.1 逐条边界规则。"""

    def setUp(self) -> None:
        self.mgr = TaskManager()

    def test_start_negative_clamped_to_zero(self) -> None:
        """``start < 0`` → 钳制为 ``0.0``。"""
        shots = [_shot("shot_001", duration=6.0)]
        result = self.mgr._normalize_clips(_clips([("shot_001", -5.0, 2.0)]), shots)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][2], 0.0)
        self.assertEqual(result[0][3], 2.0)

    def test_end_none_takes_real_duration(self) -> None:
        """``end is None`` → 取该片段真实时长。"""
        shots = [_shot("shot_001", duration=6.0)]
        result = self.mgr._normalize_clips(_clips([("shot_001", 0.0, None)]), shots)
        self.assertEqual(result[0][3], 6.0)

    def test_end_none_with_negative_start(self) -> None:
        """``start<0`` 且 ``end is None`` → ``[0, duration]``。"""
        shots = [_shot("shot_001", duration=6.0)]
        result = self.mgr._normalize_clips(_clips([("shot_001", -3.0, None)]), shots)
        self.assertEqual((result[0][2], result[0][3]), (0.0, 6.0))

    def test_end_le_start_falls_back_to_full(self) -> None:
        """``end <= start`` → 回退整段 ``[0, duration]``。"""
        shots = [_shot("shot_001", duration=6.0)]
        result = self.mgr._normalize_clips(_clips([("shot_001", 3.0, 2.0)]), shots)
        self.assertEqual((result[0][2], result[0][3]), (0.0, 6.0))
        # 退化情形：start == end
        result2 = self.mgr._normalize_clips(_clips([("shot_001", 2.0, 2.0)]), shots)
        self.assertEqual((result2[0][2], result2[0][3]), (0.0, 6.0))

    def test_end_gt_duration_clamped(self) -> None:
        """``end > duration`` → 钳制为 ``duration``（起点保留）。"""
        shots = [_shot("shot_001", duration=6.0)]
        result = self.mgr._normalize_clips(_clips([("shot_001", 1.0, 100.0)]), shots)
        self.assertEqual((result[0][2], result[0][3]), (1.0, 6.0))

    def test_start_ge_duration_falls_back_to_full(self) -> None:
        """``start >= duration`` → 回退整段。"""
        shots = [_shot("shot_001", duration=6.0)]
        for start, end in ((6.0, None), (10.0, 12.0), (6.0, 6.5)):
            result = self.mgr._normalize_clips(_clips([("shot_001", start, end)]), shots)
            self.assertEqual(
                (result[0][2], result[0][3]),
                (0.0, 6.0),
                f"start={start} end={end} 未回退整段",
            )

    def test_duration_le_zero_means_no_trim(self) -> None:
        """``duration <= 0``（探测失败）→ 视为整段、不裁剪（``end is None``）。"""
        shots = [_shot("shot_001", duration=0.0)]
        result = self.mgr._normalize_clips(_clips([("shot_001", 1.0, 2.0)]), shots)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0][2], result[0][3]), (0.0, None))

    def test_uncovered_shots_appended_in_original_order(self) -> None:
        """``clips`` 未覆盖的片段 → 宽容追加到末尾（原始顺序）。"""
        shots = [
            _shot("shot_001", duration=6.0),
            _shot("shot_002", duration=6.0),
            _shot("shot_003", duration=6.0),
        ]
        result = self.mgr._normalize_clips(_clips([("shot_002", 1.0, 2.0)]), shots)
        self.assertEqual([row[0] for row in result], ["shot_002", "shot_001", "shot_003"])
        # 追加项：整段、不裁剪
        self.assertEqual((result[1][2], result[1][3]), (0.0, 6.0))
        self.assertEqual((result[2][2], result[2][3]), (0.0, 6.0))

    def test_duplicate_shot_id_deduped_keep_first(self) -> None:
        """``shot_id`` 重复 → 去重（保留首个）。"""
        shots = [_shot("shot_001", duration=6.0)]
        clips = _clips([("shot_001", 1.0, 2.0), ("shot_001", 0.0, 0.5)])
        result = self.mgr._normalize_clips(clips, shots)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0][2], result[0][3]), (1.0, 2.0))

    def test_disabled_clips_are_excluded_and_not_reappended(self) -> None:
        """禁用片段既不参与导出，也不因「未覆盖」被追加回来。"""
        shots = [
            _shot("shot_001", duration=6.0),
            _shot("shot_002", duration=6.0),
        ]
        clips = [
            ClipEdit(shot_id="shot_001", enabled=False),
            ClipEdit(shot_id="shot_002", enabled=True, start=0.0, end=1.0),
        ]
        result = self.mgr._normalize_clips(clips, shots)
        self.assertEqual([row[0] for row in result], ["shot_002"])

    def test_failed_or_missing_shots_are_dropped(self) -> None:
        """引用不存在/失败/无 video_path 的 shot_id → 直接丢弃。"""
        shots = [
            _shot("shot_001", duration=6.0),
            _shot("shot_002", state=ShotState.FAILED, duration=6.0),
            ShotResult(shot_id="shot_003", state=ShotState.SUCCESS, video_path=None, duration=6.0),
        ]
        clips = _clips([("shot_001", 0.0, 1.0), ("shot_002", 0.0, 1.0), ("shot_003", 0.0, 1.0), ("shot_999", 0.0, 1.0)])
        result = self.mgr._normalize_clips(clips, shots)
        self.assertEqual([row[0] for row in result], ["shot_001"])

    def test_all_disabled_returns_empty(self) -> None:
        """全部 ``enabled=False`` → 归一化结果为空（由 export 层抛 SYS_BAD_REQUEST）。"""
        shots = [_shot("shot_001", duration=6.0), _shot("shot_002", duration=6.0)]
        clips = [
            ClipEdit(shot_id="shot_001", enabled=False),
            ClipEdit(shot_id="shot_002", enabled=False),
        ]
        self.assertEqual(self.mgr._normalize_clips(clips, shots), [])

    def test_empty_clips_still_appends_all_available(self) -> None:
        """``clips=[]`` → 全部可用片段按原序追加（宽容），并非空。"""
        shots = [_shot("shot_001", duration=6.0), _shot("shot_002", duration=6.0)]
        result = self.mgr._normalize_clips([], shots)
        self.assertEqual([row[0] for row in result], ["shot_001", "shot_002"])

    def test_order_field_stable_sort(self) -> None:
        """提供 ``order`` 时按 ``(order is None, order)`` 稳定排序。"""
        shots = [
            _shot("shot_001", duration=6.0),
            _shot("shot_002", duration=6.0),
            _shot("shot_003", duration=6.0),
        ]
        # A=shot_001(order=2) B=shot_002(order=None) C=shot_003(order=0) → [C, A, B]
        clips = [
            ClipEdit(shot_id="shot_001", order=2),
            ClipEdit(shot_id="shot_002", order=None),
            ClipEdit(shot_id="shot_003", order=0),
        ]
        result = self.mgr._normalize_clips(clips, shots)
        self.assertEqual([row[0] for row in result], ["shot_003", "shot_001", "shot_002"])

    def test_no_order_keeps_array_order(self) -> None:
        """未提供 ``order`` 时严格按 ``clips`` 数组顺序。"""
        shots = [
            _shot("shot_001", duration=6.0),
            _shot("shot_002", duration=6.0),
            _shot("shot_003", duration=6.0),
        ]
        clips = _clips([("shot_003", 0.0, 1.0), ("shot_001", 0.0, 1.0), ("shot_002", 0.0, 1.0)])
        result = self.mgr._normalize_clips(clips, shots)
        self.assertEqual([row[0] for row in result], ["shot_003", "shot_001", "shot_002"])


class TestNormalizeClipsSplit(unittest.TestCase):
    """增量：split（有效 id = ``clip_id or shot_id``）与第 5 位 ``clip_id``。"""

    def setUp(self) -> None:
        self.mgr = TaskManager()

    def test_split_segments_both_kept(self) -> None:
        """同一 shot 的多段（clip_id 互异）均保留，各自携带起止。"""
        shots = [_shot("shot_001", duration=10.0)]
        clips = [
            ClipEdit(shot_id="shot_001", clip_id="shot_001#1", start=0.0, end=4.0),
            ClipEdit(shot_id="shot_001", clip_id="shot_001#2", start=4.0, end=10.0),
        ]
        result = self.mgr._normalize_clips(clips, shots)
        self.assertEqual(len(result), 2)
        self.assertEqual([r[4] for r in result], ["shot_001#1", "shot_001#2"])
        self.assertEqual([(r[2], r[3]) for r in result], [(0.0, 4.0), (4.0, 10.0)])

    def test_split_does_not_produce_ghost_base_clip(self) -> None:
        """split 后基础 shot 只被「覆盖」一次 → 不会被宽容追加成幽灵片段。"""
        shots = [_shot("shot_001", duration=10.0)]
        clips = [
            ClipEdit(shot_id="shot_001", clip_id="shot_001#1", start=0.0, end=4.0),
            ClipEdit(shot_id="shot_001", clip_id="shot_001#2", start=4.0, end=10.0),
        ]
        result = self.mgr._normalize_clips(clips, shots)
        self.assertEqual(len(result), 2, "不应出现第 3 个基础幽灵片段")
        self.assertTrue(all(r[0] == "shot_001" for r in result))

    def test_effective_id_dedup_keeps_first(self) -> None:
        """同一有效 id（clip_id 相同）重复 → 去重保留首个。"""
        shots = [_shot("shot_001", duration=10.0)]
        clips = [
            ClipEdit(shot_id="shot_001", clip_id="shot_001#1", start=0.0, end=3.0),
            ClipEdit(shot_id="shot_001", clip_id="shot_001#1", start=5.0, end=8.0),
        ]
        result = self.mgr._normalize_clips(clips, shots)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0][2], result[0][3]), (0.0, 3.0))

    def test_disabled_split_segment_does_not_affect_other(self) -> None:
        """禁用其中一段 → 只影响该段，另一段照常导出。"""
        shots = [_shot("shot_001", duration=10.0)]
        clips = [
            ClipEdit(shot_id="shot_001", clip_id="shot_001#1", enabled=False, start=0.0, end=4.0),
            ClipEdit(shot_id="shot_001", clip_id="shot_001#2", enabled=True, start=4.0, end=10.0),
        ]
        result = self.mgr._normalize_clips(clips, shots)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][4], "shot_001#2")
        self.assertEqual((result[0][2], result[0][3]), (4.0, 10.0))

    def test_no_clip_id_is_backward_compatible(self) -> None:
        """缺省 ``clip_id`` → 第 5 位为 None，前 4 位与旧 4 元组逐字一致。"""
        shots = [_shot("shot_001", duration=6.0)]
        result = self.mgr._normalize_clips(_clips([("shot_001", 1.0, 2.0)]), shots)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0][4])
        self.assertEqual(tuple(result[0][:4]), ("shot_001", "shots/x.mp4", 1.0, 2.0))

    def test_two_same_shot_without_clip_id_dedup(self) -> None:
        """两条同 ``shot_id`` 且都无 ``clip_id`` → 有效 id 均为 shot_id → 去重保留首个。"""
        shots = [_shot("shot_001", duration=6.0)]
        clips = _clips([("shot_001", 1.0, 2.0), ("shot_001", 0.0, 0.5)])
        result = self.mgr._normalize_clips(clips, shots)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0][2], result[0][3]), (1.0, 2.0))


class TestExportEditPlanErrors(unittest.TestCase):
    """``export_edit_plan`` 的异常路径（错误码对齐设计 §3.4）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_export_err_"))
        self.mgr = TaskManager()
        self.tid = self.mgr.create_task(self.tmp, Config())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_task_not_found(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            self.mgr.export_edit_plan("no-such-task", ExportRequest(clips=[]))
        self.assertIn(ErrorCode.TASK_NOT_FOUND, str(ctx.exception))

    def test_script_missing_raises_task_failed(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            self.mgr.export_edit_plan(self.tid, ExportRequest(clips=[ClipEdit(shot_id="shot_001")]))
        self.assertIn(ErrorCode.TASK_FAILED, str(ctx.exception))

    def test_no_successful_shots_raises_bad_request(self) -> None:
        self.mgr._records[self.tid].script = ScriptDocument(title="t", shots=[Shot(shot_id="shot_001")])
        self.mgr.update_shot(
            self.tid, ShotResult(shot_id="shot_001", state=ShotState.FAILED, duration=0.0)
        )
        with self.assertRaises(KeyError) as ctx:
            self.mgr.export_edit_plan(self.tid, ExportRequest(clips=[ClipEdit(shot_id="shot_001")]))
        self.assertIn(ErrorCode.SYS_BAD_REQUEST, str(ctx.exception))
        self.assertIn("没有可导出的片段", str(ctx.exception))

    def test_empty_clips_with_no_available_shots_raises(self) -> None:
        """``clips=[]`` 且无任何可用镜头 → ``SYS_BAD_REQUEST``。

        注意：``clips=[]`` **并非无条件报错**——若存在可用镜头，按宽容规则会全部追加
        并正常导出。只有「归一化后为空」才触发该错误。
        """
        self.mgr._records[self.tid].script = ScriptDocument(title="t", shots=[Shot(shot_id="shot_001")])
        self.mgr.update_shot(
            self.tid, ShotResult(shot_id="shot_001", state=ShotState.FAILED, duration=0.0)
        )
        with self.assertRaises(KeyError) as ctx:
            self.mgr.export_edit_plan(self.tid, ExportRequest(clips=[]))
        self.assertIn(ErrorCode.SYS_BAD_REQUEST, str(ctx.exception))


@unittest.skipUnless(_FFMPEG_AVAILABLE, "FFmpeg 不可用，跳过真实导出测试")
class TestExportIntegration(unittest.TestCase):
    """真实裁剪 + 拼接导出，并独立复核 ``final_video.mp4`` 未被污染（AC9）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_export_real_"))
        (self.tmp / "shots").mkdir(parents=True, exist_ok=True)
        self.mgr = TaskManager()
        self.tid = self.mgr.create_task(self.tmp, Config())
        self.rel = {}
        abs_paths = []
        for index in (1, 2, 3):
            shot_id = f"shot_{index:03d}"
            rel = f"shots/{shot_id}.mp4"
            abs_path = self.tmp / rel
            make_mock_video(
                str(abs_path), header=f"SHOT {index} / 3", duration=2, width=320, height=180
            )
            self.rel[shot_id] = rel
            abs_paths.append(str(abs_path))
            self.mgr.update_shot(
                self.tid,
                ShotResult(
                    shot_id=shot_id,
                    state=ShotState.SUCCESS,
                    video_path=rel,
                    duration=2.0,
                ),
            )
        self.mgr._records[self.tid].script = ScriptDocument(
            title="t", shots=[Shot(shot_id=sid) for sid in self.rel]
        )
        self.final = self.tmp / "final_video.mp4"
        concat_videos(abs_paths, str(self.final))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_edl_and_final_video_untouched(self) -> None:
        before_hash = hashlib.md5(self.final.read_bytes()).hexdigest()
        before_mtime = self.final.stat().st_mtime_ns
        before_size = self.final.stat().st_size

        request = ExportRequest(
            clips=[
                ClipEdit(shot_id="shot_003", enabled=True, start=0.0, end=1.0, order=0),
                ClipEdit(shot_id="shot_002", enabled=False, order=1),
                ClipEdit(shot_id="shot_001", enabled=True, start=0.5, end=1.5, order=2),
            ]
        )
        export_rel, plan_rel, count, total = self.mgr.export_edit_plan(self.tid, request)

        # 产物存在且为任务相对路径
        self.assertTrue(export_rel.startswith("exports/export_"))
        self.assertTrue(plan_rel.startswith("exports/edit_plan_"))
        export_abs = self.tmp / export_rel
        plan_abs = self.tmp / plan_rel
        self.assertTrue(export_abs.exists())
        self.assertTrue(plan_abs.exists())
        # 配对命名：edit_plan 与 export 后缀一致
        self.assertEqual(
            plan_abs.stem[len("edit_plan_"):],
            export_abs.stem[len("export_"):],
        )

        self.assertEqual(count, 2)
        self.assertAlmostEqual(total, 2.0, delta=0.3)
        self.assertAlmostEqual(probe_duration(str(export_abs)), 2.0, delta=0.3)

        # final_video.mp4 三要素（内容哈希 / mtime / size）完全不变
        self.assertEqual(hashlib.md5(self.final.read_bytes()).hexdigest(), before_hash)
        self.assertEqual(self.final.stat().st_mtime_ns, before_mtime)
        self.assertEqual(self.final.stat().st_size, before_size)

    def test_export_plan_json_content(self) -> None:
        import json

        request = ExportRequest(
            clips=[ClipEdit(shot_id="shot_001", enabled=True, start=0.5, end=1.2)]
        )
        _export_rel, plan_rel, count, _total = self.mgr.export_edit_plan(self.tid, request)
        self.assertEqual(count, 3)  # shot_001 裁剪 + shot_002/003 追加
        plan = json.loads((self.tmp / plan_rel).read_text(encoding="utf-8"))
        self.assertEqual(plan["clip_count"], 3)
        self.assertEqual(plan["clips"][0]["shot_id"], "shot_001")
        self.assertAlmostEqual(plan["clips"][0]["start"], 0.5, places=3)
        self.assertAlmostEqual(plan["clips"][0]["end"], 1.2, places=3)
        self.assertAlmostEqual(plan["clips"][0]["duration"], 0.7, places=3)

    def test_export_plan_clips_include_clip_id(self) -> None:
        """``plan_clips[]`` 补齐 ``clip_id``（旧解析方忽略即可）；未分割段为 None。"""
        import json

        request = ExportRequest(
            clips=[
                ClipEdit(shot_id="shot_001", clip_id="shot_001#1", enabled=True, start=0.0, end=1.0),
                ClipEdit(shot_id="shot_001", clip_id="shot_001#2", enabled=True, start=1.0, end=2.0),
            ]
        )
        _export_rel, plan_rel, count, _total = self.mgr.export_edit_plan(self.tid, request)
        # 2 个 split 段 + 未覆盖的 shot_002/003 被宽容追加
        self.assertEqual(count, 4)
        plan = json.loads((self.tmp / plan_rel).read_text(encoding="utf-8"))
        self.assertEqual(
            [c["clip_id"] for c in plan["clips"][:2]], ["shot_001#1", "shot_001#2"]
        )
        self.assertIsNone(plan["clips"][2]["clip_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
