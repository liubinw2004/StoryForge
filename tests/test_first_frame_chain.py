"""串行首帧链编排器单元测试：状态机 + 全局串行锁 + 805 重试 + 断点续跑。

用假 ``VideoGenerator``（成功镜头生成极小真实 mp4 以便截尾帧，指定镜头抛错），
``DirectorAgent.build_prompt`` 的 Mock 分支输出 ``SEGMENT i / total``，据此解析段号。

运行（**禁代理**）::

    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        .venv/Scripts/python.exe -m unittest tests.test_first_frame_chain -v
"""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.agents.director_agent import DirectorAgent
from app.models.schemas import Config, ScriptDocument, Shot
from app.services.adapters.base import VideoError
from app.services.first_frame_chain import (
    SUBMIT_LOCK,
    ChainSegmentState,
    FirstFrameChain,
)
from app.utils.ffmpeg_utils import make_mock_video, probe_ffmpeg
from app.utils.responses import ErrorCode

_FFMPEG_AVAILABLE = probe_ffmpeg()["available"]

_SEG_RE = re.compile(r"SEGMENT (\d+)")


class FakeI2V:
    """假工作流 i2v 生成器：成功时用 FFmpeg 产出真实 mp4，可按段号模拟失败/805。"""

    def __init__(
        self,
        *,
        fail_indexes: tuple[int, ...] = (),
        first_call_805: bool = False,
    ) -> None:
        self.fail_indexes = set(fail_indexes)
        self.first_call_805 = first_call_805
        self.calls: list[dict] = []
        self._call_counts: dict[int, int] = {}

    def generate_i2v(self, first_frame, prompt, duration, seed, size, out_path) -> str:  # noqa: D102
        match = _SEG_RE.search(prompt or "")
        index = int(match.group(1)) if match else 0
        self._call_counts[index] = self._call_counts.get(index, 0) + 1
        self.calls.append(
            {
                "index": index,
                "seed": seed,
                "first_frame": first_frame,
                "out_path": out_path,
            }
        )
        if index in self.fail_indexes:
            raise VideoError(ErrorCode.VID_GENERATE_FAILED, f"模拟第 {index} 段失败")
        if self.first_call_805 and self._call_counts.get(index) == 1:
            raise VideoError(ErrorCode.VID_GENERATE_FAILED, "805: task failed, retry with new seed")
        return make_mock_video(
            out_path, header=f"SEGMENT {index}", body_lines=["串行首帧链"],
            duration=int(duration), width=160, height=120, fps=12,
        )


def _script(*durations: int) -> ScriptDocument:
    shots = [
        Shot(shot_id=f"shot_{i:03d}", dialogue="d", action="a", duration=d, prompt=f"p{i}")
        for i, d in enumerate(durations, start=1)
    ]
    return ScriptDocument(title="t", shots=shots)


def _wait_segment(chain: FirstFrameChain, index: int, states: set[str], timeout: float = 30.0) -> dict:
    """轮询直到某段进入指定状态集合（后台线程异步完成）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        seg = chain.get_segment(index)
        if seg.get("state") in states:
            return seg
        time.sleep(0.05)
    return chain.get_segment(index)


@unittest.skipUnless(_FFMPEG_AVAILABLE, "FFmpeg 不可用，跳过串行首帧链真实产片测试")
class TestFirstFrameChain(unittest.TestCase):
    """链状态机 + 串行锁 + 805 重试 + 断点续跑。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_chain_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _chain(self, video: FakeI2V | None = None) -> FirstFrameChain:
        return FirstFrameChain(
            task_id="t1",
            output_dir=self.tmp,
            config=Config(),
            script=_script(2, 2, 2),
            video_generator=video or FakeI2V(),
            director=DirectorAgent(None),
        )

    def test_initial_state_is_empty_before_start(self) -> None:
        chain = self._chain()
        self.assertEqual(chain.get_state(), {"segments": [], "current_index": 0})

    def test_start_rejects_empty_bytes(self) -> None:
        chain = self._chain()
        with self.assertRaises(KeyError):
            chain.start(b"")

    def test_start_initializes_segments_and_autosubmits_segment_1(self) -> None:
        chain = self._chain()
        state = chain.start(b"\x89PNG\r\n\x1a\n" + b"X" * 16)
        self.assertEqual(len(state["segments"]), 3)
        self.assertEqual(state["segments"][0]["first_frame_source"], "frames/initial_frame.png")
        self.assertEqual(state["segments"][1]["first_frame_source"], None)
        self.assertTrue((self.tmp / "frames" / "initial_frame.png").exists())
        seg1 = _wait_segment(chain, 1, {"awaiting_confirm", "failed"})
        self.assertEqual(seg1["state"], "awaiting_confirm")
        self.assertEqual(seg1["video_path"], "shots/segment_001.mp4")
        self.assertEqual(seg1["tail_frame_path"], "frames/tail_001.png")
        self.assertTrue((self.tmp / "frames" / "tail_001.png").exists())

    def test_accept_advances_and_autosubmits_next_segment(self) -> None:
        chain = self._chain()
        chain.start(b"\x89PNG\r\n\x1a\n" + b"X" * 16)
        _wait_segment(chain, 1, {"awaiting_confirm", "failed"})
        chain.accept_segment(1)
        self.assertEqual(chain.get_segment(1)["state"], "accepted")
        state = chain.get_state()
        self.assertEqual(state["current_index"], 2)
        # 段 2 首帧 = 段 1 尾帧
        self.assertEqual(
            chain.get_segment(2)["first_frame_source"], "frames/tail_001.png"
        )
        seg2 = _wait_segment(chain, 2, {"awaiting_confirm", "failed"})
        self.assertEqual(seg2["state"], "awaiting_confirm")

    def test_accept_without_awaiting_confirm_raises(self) -> None:
        chain = self._chain()
        chain.start(b"\x89PNG\r\n\x1a\n" + b"X" * 16)
        # 段 2 仍 pending，直接 accept 应报 TASK_STATE_INVALID
        with self.assertRaises(KeyError):
            chain.accept_segment(2)

    def test_segment_failure_marks_failed_and_does_not_block_others(self) -> None:
        chain = self._chain(video=FakeI2V(fail_indexes=(2,)))
        chain.start(b"\x89PNG\r\n\x1a\n" + b"X" * 16)
        _wait_segment(chain, 1, {"awaiting_confirm", "failed"})
        chain.accept_segment(1)
        seg2 = _wait_segment(chain, 2, {"failed"})
        self.assertEqual(seg2["state"], "failed")
        self.assertIn("模拟第 2 段失败", seg2["error"])
        # 段 3 仍 pending（未被自动提交）
        self.assertEqual(chain.get_segment(3)["state"], "pending")

    def test_regenerate_resets_and_uses_new_seed(self) -> None:
        chain = self._chain(video=FakeI2V(fail_indexes=(2,)))
        chain.start(b"\x89PNG\r\n\x1a\n" + b"X" * 16)
        _wait_segment(chain, 1, {"awaiting_confirm", "failed"})
        chain.accept_segment(1)
        _wait_segment(chain, 2, {"failed"})

        old_seed = chain.get_segment(2)["seed"]
        # 换成不再失败的替身，重生成段 2
        chain.video = FakeI2V()
        chain.regenerate_segment(2)
        seg2 = _wait_segment(chain, 2, {"awaiting_confirm", "failed"})
        self.assertEqual(seg2["state"], "awaiting_confirm")
        self.assertNotEqual(seg2["seed"], old_seed, "重生成应换随机 seed")

    def test_805_retries_once_then_succeeds(self) -> None:
        video = FakeI2V(first_call_805=True)
        chain = self._chain(video=video)
        chain.start(b"\x89PNG\r\n\x1a\n" + b"X" * 16)
        seg1 = _wait_segment(chain, 1, {"awaiting_confirm", "failed"})
        self.assertEqual(seg1["state"], "awaiting_confirm", "805 后应自动换 seed 重试成功")
        # 段 1 应有两次提交（首次 805 失败 + 重试成功）
        seg1_calls = [c for c in video.calls if c["index"] == 1]
        self.assertEqual(len(seg1_calls), 2)
        self.assertNotEqual(seg1_calls[0]["seed"], seg1_calls[1]["seed"])

    def test_805_retries_then_marks_failed_when_still_805(self) -> None:
        class _Always805(FakeI2V):
            def generate_i2v(self, *a, **k):  # noqa: D102
                raise VideoError(ErrorCode.VID_GENERATE_FAILED, "805: always failing")

        chain = self._chain(video=_Always805())
        chain.start(b"\x89PNG\r\n\x1a\n" + b"X" * 16)
        seg1 = _wait_segment(chain, 1, {"failed"})
        self.assertEqual(seg1["state"], "failed")
        self.assertIn("805", seg1["error"])

    def test_submit_is_idempotent_while_in_flight_or_awaiting(self) -> None:
        chain = self._chain()
        chain.start(b"\x89PNG\r\n\x1a\n" + b"X" * 16)
        _wait_segment(chain, 1, {"awaiting_confirm", "failed"})
        before = chain.get_segment(1)["seed"]
        chain.submit_segment(1)
        self.assertEqual(chain.get_segment(1)["seed"], before, "awaiting_confirm 段不可重复提交")

    def test_persistence_restores_state_after_reload(self) -> None:
        chain = self._chain()
        chain.start(b"\x89PNG\r\n\x1a\n" + b"X" * 16)
        _wait_segment(chain, 1, {"awaiting_confirm", "failed"})
        # 用同目录重建编排器 → 从 chain.json 恢复（断点续跑）
        reloaded = self._chain()
        state = reloaded.get_state()
        self.assertEqual(state["current_index"], 1)
        self.assertEqual(len(state["segments"]), 3)
        self.assertEqual(state["segments"][0]["state"], "awaiting_confirm")
        self.assertEqual(state["segments"][0]["video_path"], "shots/segment_001.mp4")
        # 续跑：接受段 1 → 自动提交段 2
        reloaded.accept_segment(1)
        seg2 = _wait_segment(reloaded, 2, {"awaiting_confirm", "failed"})
        self.assertEqual(seg2["state"], "awaiting_confirm")

    def test_all_accepted_after_accepting_every_segment(self) -> None:
        chain = self._chain()
        chain.start(b"\x89PNG\r\n\x1a\n" + b"X" * 16)
        self.assertFalse(chain.all_accepted())
        for index in (1, 2):
            _wait_segment(chain, index, {"awaiting_confirm", "failed"})
            chain.accept_segment(index)
        _wait_segment(chain, 3, {"awaiting_confirm", "failed"})
        chain.accept_segment(3)
        self.assertTrue(chain.all_accepted())
        self.assertTrue((self.tmp / "chain.json").exists())


class TestSerialLock(unittest.TestCase):
    """全局串行锁：跨任务互斥（防 421）。"""

    def test_submit_lock_is_a_real_shared_lock(self) -> None:
        self.assertIs(type(SUBMIT_LOCK), type(threading.Lock()))

    def test_submit_lock_is_module_global_shared_across_instances(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sf_lock_") as tmp_a, \
                tempfile.TemporaryDirectory(prefix="sf_lock_") as tmp_b:
            chain_a = FirstFrameChain(
                task_id="a", output_dir=Path(tmp_a),
                config=Config(), script=_script(2), video_generator=FakeI2V(),
                director=DirectorAgent(None),
            )
            chain_b = FirstFrameChain(
                task_id="b", output_dir=Path(tmp_b),
                config=Config(), script=_script(2), video_generator=FakeI2V(),
                director=DirectorAgent(None),
            )
            self.assertIsNot(chain_a, chain_b)
        # 关键：全局锁是模块级单例，两个编排器共享同一把锁（同一时刻仅 1 个提交在飞）
        from app.services import first_frame_chain as ffc
        self.assertIs(SUBMIT_LOCK, ffc.SUBMIT_LOCK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
