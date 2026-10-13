"""增量补充测试：``trim_clip`` 裁剪精度的**独立复核**。

不复用工程师给定的 3 组样本，自行另选 3 组（含「不裁剪」路径与「接近片段结尾」边界），
用 ``probe_duration`` 实测，输出 期望 vs 实测 vs 误差。

运行：``.venv/Scripts/python.exe -m unittest tests.test_trim_precision -v``
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from app.utils.ffmpeg_utils import make_mock_video, probe_duration, trim_clip

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None

_SOURCE_SECONDS = 6.0

# (用例名, start, end, 期望时长)
_CASES = [
    ("不裁剪路径 start=0,end=None", 0.0, None, _SOURCE_SECONDS),
    ("中部裁剪 start=1.3,end=3.7", 1.3, 3.7, 2.4),
    ("接近结尾 start=4.5,end=6.0", 4.5, 6.0, 1.5),
]


@unittest.skipUnless(_FFMPEG_AVAILABLE, "FFmpeg 不可用，跳过裁剪精度测试")
class TestTrimPrecision(unittest.TestCase):
    """裁剪精度独立复核（容差 ±0.1s）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_trim_prec_"))
        self.src = self.tmp / "src.mp4"
        make_mock_video(
            str(self.src), header="SOURCE", duration=int(_SOURCE_SECONDS), width=320, height=180
        )
        self.src_duration = probe_duration(str(self.src))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_source_duration_precondition(self) -> None:
        self.assertAlmostEqual(self.src_duration, _SOURCE_SECONDS, delta=0.2)

    def test_precision_matrix(self) -> None:
        print(f"\n  源视频实测时长 = {self.src_duration:.3f}s")
        print(f"  {'用例':<34}{'期望':>8}{'实测':>10}{'误差':>10}")
        worst = 0.0
        for name, start, end, expected in _CASES:
            out = self.tmp / f"out_{abs(hash(name)) % 10000}.mp4"
            trim_clip(str(self.src), str(out), start, end)
            actual = probe_duration(str(out))
            error = abs(actual - expected)
            worst = max(worst, error)
            print(f"  {name:<34}{expected:>8.3f}{actual:>10.3f}{error:>10.3f}")
            self.assertLessEqual(error, 0.1, f"{name} 误差 {error:.3f}s 超出容差")
        print(f"  最大误差 = {worst:.3f}s")

    def test_no_trim_path_is_byte_identical_copy(self) -> None:
        """``start=0,end=None`` → 走 ``shutil.copy2`` 直拷，字节与源完全一致。"""
        out = self.tmp / "copy.mp4"
        trim_clip(str(self.src), str(out), 0.0, None)
        self.assertEqual(out.read_bytes(), self.src.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
