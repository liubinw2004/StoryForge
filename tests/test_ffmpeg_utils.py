"""``app/utils/ffmpeg_utils.py`` 单元测试。

覆盖：
- **FFmpeg 缺失时错误码必须是 ``FFM_NOT_FOUND``**（修复 F2，PATH 覆盖手法）；
- 正常环境下 Mock 视频真实产片 + 时长校验、抽帧、拼接；
- ``probe_ffmpeg()`` 在 PATH 剔除后的诊断结果。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils.ffmpeg_utils import (
    FFmpegError,
    concat_videos,
    extract_last_frame,
    extract_tail_frame,
    make_mock_video,
    probe_duration,
    probe_ffmpeg,
)
from app.utils.responses import ErrorCode

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None


# --------------------------------------------------------------------------- #
# 独立取证工具（第二轮 QA 补回：规格 / 魔数 / 3×3 拼接 / 进度条动画）
# --------------------------------------------------------------------------- #
def _ffprobe_stream(path: str) -> dict:
    """用 ffprobe 读取第一条视频流的编码 / 分辨率 / 帧率。"""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate",
            "-of", "default=nw=1", path,
        ],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    result: dict = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _raw_frame_bytes(path: str, timestamp: float) -> bytes:
    """抽取指定时间点的原始 RGB 帧字节（用于比较进度条是否移动）。"""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(timestamp), "-i", path,
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=90,
    )
    return proc.stdout


def _diff_ratio(a: bytes, b: bytes) -> float:
    """返回两段等长 RGB 字节中不同像素的比例。"""
    if not a or not b:
        return 1.0
    n = min(len(a), len(b))
    diff = sum(1 for i in range(0, n, 3) if a[i:i + 3] != b[i:i + 3])
    return diff / max(1, n // 3)


class TestFfmpegMissing(unittest.TestCase):
    """F2：FFmpeg 二进制不存在时，错误码必须为 FFM_NOT_FOUND（而非 FFM_EXEC_FAILED）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.empty_path_dir = os.path.join(self._tmp.name, "empty_path")
        os.makedirs(self.empty_path_dir, exist_ok=True)
        self.out_dir = os.path.join(self._tmp.name, "out")
        os.makedirs(self.out_dir, exist_ok=True)

    def _stripped_path(self):
        """上下文：把 PATH 换成不含 ffmpeg 的空目录，并禁用包内 bin/ 解析。

        由于生产代码优先解析「包内 bin/」（开发机若存在 ``<project_root>/bin``
        也会被命中），此处同时把 ``_bundled_bin_dir`` 补丁为 ``None``，以隔离
        出「PATH 中确实没有 ffmpeg」这一被测条件。
        """
        import contextlib
        from unittest import mock

        from app.utils import ffmpeg_utils

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, {"PATH": self.empty_path_dir}))
        stack.enter_context(mock.patch.object(ffmpeg_utils, "_bundled_bin_dir", return_value=None))
        return stack

    def test_precondition_ffmpeg_not_discoverable(self):
        with self._stripped_path():
            self.assertIsNone(shutil.which("ffmpeg"), "PATH 覆盖失效，测试前置条件不成立")

    def test_make_mock_video_raises_ffm_not_found(self):
        out = os.path.join(self.out_dir, "mock.mp4")
        with self._stripped_path():
            with self.assertRaises(FFmpegError) as ctx:
                make_mock_video(out, header="SHOT 1 / 1", body_lines=["测试"], duration=1)
        self.assertEqual(ctx.exception.code, ErrorCode.FFM_NOT_FOUND)
        self.assertIn("FFM_NOT_FOUND", str(ctx.exception))
        self.assertIn("FFmpeg", str(ctx.exception))

    def test_extract_last_frame_raises_ffm_not_found(self):
        with self._stripped_path():
            with self.assertRaises(FFmpegError) as ctx:
                extract_last_frame("no_such_video.mp4", os.path.join(self.out_dir, "f.jpg"))
        self.assertEqual(ctx.exception.code, ErrorCode.FFM_NOT_FOUND)

    def test_extract_tail_frame_raises_ffm_not_found(self):
        with self._stripped_path():
            with self.assertRaises(FFmpegError) as ctx:
                extract_tail_frame("no_such_video.mp4", os.path.join(self.out_dir, "tail.png"))
        self.assertEqual(ctx.exception.code, ErrorCode.FFM_NOT_FOUND)

    def test_concat_videos_raises_ffm_not_found(self):
        with self._stripped_path():
            with self.assertRaises(FFmpegError) as ctx:
                concat_videos(["no_such_shot.mp4"], os.path.join(self.out_dir, "final.mp4"))
        self.assertEqual(ctx.exception.code, ErrorCode.FFM_NOT_FOUND)

    def test_concat_videos_empty_input_uses_concat_failed(self):
        """无输入属于「参数问题」，错误码应保持 FFM_CONCAT_FAILED（不受本次修复影响）。"""
        with self.assertRaises(FFmpegError) as ctx:
            concat_videos([], os.path.join(self.out_dir, "final.mp4"))
        self.assertEqual(ctx.exception.code, ErrorCode.FFM_CONCAT_FAILED)

    def test_probe_ffmpeg_reports_unavailable(self):
        with self._stripped_path():
            info = probe_ffmpeg()
        self.assertFalse(info["available"])
        self.assertIn("FFmpeg", info["message"])


@unittest.skipUnless(_FFMPEG_AVAILABLE, "FFmpeg 不可用，跳过真实产片测试")
class TestFfmpegExecution(unittest.TestCase):
    """FFmpeg 可用时的真实执行回归（保持原有能力不变）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_make_mock_video_produces_mp4_with_expected_duration(self):
        out = os.path.join(self._tmp.name, "shots", "shot_001.mp4")
        produced = make_mock_video(
            out,
            header="SHOT 1 / 2",
            body_lines=["场景：咖啡馆", "对白：你好"],
            duration=2,
            width=640,
            height=360,
        )
        self.assertTrue(os.path.exists(produced))
        self.assertGreater(os.path.getsize(produced), 0)
        self.assertAlmostEqual(probe_duration(produced), 2.0, delta=0.5)

    def test_extract_last_frame_and_concat(self):
        shots = []
        for index in (1, 2):
            out = os.path.join(self._tmp.name, f"shot_{index:03d}.mp4")
            make_mock_video(out, header=f"SHOT {index} / 2", body_lines=[], duration=2,
                            width=640, height=360)
            shots.append(out)

        frame = os.path.join(self._tmp.name, "frames", "frame_001.jpg")
        extract_last_frame(shots[0], frame)
        self.assertTrue(os.path.exists(frame))
        self.assertGreater(os.path.getsize(frame), 0)

        final = concat_videos(shots, os.path.join(self._tmp.name, "final_video.mp4"))
        self.assertTrue(os.path.exists(final))
        self.assertAlmostEqual(probe_duration(final), 4.0, delta=0.5)

    def test_probe_ffmpeg_available(self):
        info = probe_ffmpeg()
        self.assertTrue(info["available"])
        self.assertTrue(info["version"])

    def test_extract_tail_frame_produces_png(self):
        """串行首帧链：从视频抽最后一帧输出 PNG（供下一段作首帧）。"""
        video = make_mock_video(
            os.path.join(self._tmp.name, "seg.mp4"),
            header="SEGMENT 1 / 3", body_lines=["尾帧"], duration=2, width=320, height=180,
        )
        tail = os.path.join(self._tmp.name, "frames", "tail_001.png")
        produced = extract_tail_frame(video, tail)
        self.assertTrue(os.path.exists(produced))
        self.assertGreater(os.path.getsize(produced), 0)
        with open(produced, "rb") as handle:
            magic = handle.read(8)
        # PNG 魔数：89 50 4E 47 0D 0A 1A 0A
        self.assertEqual(magic, b"\x89PNG\r\n\x1a\n", "尾帧应为合法 PNG")


@unittest.skipUnless(_FFMPEG_AVAILABLE, "FFmpeg 不可用，跳过真实产片测试")
class TestMockVideoSpecRegression(unittest.TestCase):
    """第二轮 QA 补回：原被覆盖测试文件中的「规格 / 魔数 / 3×3 拼接 / 进度条动画」证据。

    对应 PRD §3.2.2 / ARCHITECTURE T02：Mock 必须 1280x720 / 24fps / mp4(H.264)，
    含渐变背景 + 底部**随时间增长**的进度条；抽帧须为合法 JPEG；拼接时长 ≈ Σ。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_mock_video_spec_1280x720_24fps_h264(self):
        out = os.path.join(self._tmp.name, "shot.mp4")
        produced = make_mock_video(
            out, header="SHOT 1 / 3",
            body_lines=["场景：咖啡馆", "对白：你好", "画面：推门而入"],
            duration=2, width=1280, height=720, fps=24,
        )
        self.assertTrue(os.path.exists(produced))
        self.assertGreater(os.path.getsize(produced), 1000)
        info = _ffprobe_stream(produced)
        self.assertEqual(info.get("codec_name"), "h264")
        self.assertEqual(info.get("width"), "1280")
        self.assertEqual(info.get("height"), "720")
        self.assertEqual(info.get("r_frame_rate"), "24/1")
        self.assertAlmostEqual(probe_duration(produced), 2.0, delta=0.35)

    def test_extract_last_frame_is_valid_jpeg(self):
        video = make_mock_video(os.path.join(self._tmp.name, "v.mp4"),
                                header="SHOT 1 / 1", duration=2, width=320, height=180)
        jpg = os.path.join(self._tmp.name, "frame_001.jpg")
        produced = extract_last_frame(video, jpg)
        with open(produced, "rb") as handle:
            magic = handle.read(2)
        self.assertEqual(magic, b"\xff\xd8", "末帧应为合法 JPEG（FFD8 魔数）")

    def test_concat_three_by_three_is_nine_seconds(self):
        parts = []
        for index in (1, 2, 3):
            parts.append(make_mock_video(
                os.path.join(self._tmp.name, f"shot_{index:03d}.mp4"),
                header=f"SHOT {index} / 3", body_lines=[f"第 {index} 个镜头"],
                duration=3, width=320, height=180,
            ))
        final = concat_videos(parts, os.path.join(self._tmp.name, "final_video.mp4"))
        self.assertTrue(os.path.exists(final))
        self.assertAlmostEqual(probe_duration(final), 9.0, delta=0.5)

    def test_progress_bar_animates(self):
        """抽 0.1s 与 1.8s 帧比较：底部进度条应随时间增长（帧内容不同）。"""
        out = os.path.join(self._tmp.name, "bar.mp4")
        make_mock_video(out, header="SHOT 1 / 1", body_lines=["进度条测试"],
                        duration=2, width=640, height=360, fps=24)
        early = _raw_frame_bytes(out, 0.1)
        late = _raw_frame_bytes(out, 1.8)
        self.assertTrue(early and late, "应能抽到原始帧")
        self.assertGreater(_diff_ratio(early, late), 0.01, "首末帧差异过小，进度条可能未动")

    def test_mock_video_is_not_uniform_color(self):
        """画面应为渐变 + 叠加元素（非纯色），保证镜头可辨识。"""
        out = os.path.join(self._tmp.name, "grad.mp4")
        make_mock_video(out, header="SHOT 2 / 3", body_lines=[], duration=1,
                        width=320, height=180)
        frame = _raw_frame_bytes(out, 0.5)
        self.assertTrue(frame)
        self.assertGreater(max(frame) - min(frame), 20, "画面近似纯色，疑似渐变背景缺失")

    def test_probe_duration_missing_file_returns_zero(self):
        """`probe_duration()` 对不存在的文件应降级返回 0.0（而非抛异常）。"""
        self.assertEqual(probe_duration(os.path.join(self._tmp.name, "nope.mp4")), 0.0)


if __name__ == "__main__":
    unittest.main()
