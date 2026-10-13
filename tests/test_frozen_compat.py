"""冻结（PyInstaller）兼容性单元测试。

验证 :mod:`app.utils.paths` 在 ``sys.frozen`` 为真时：
- ``resolve_project_root()`` 返回 **exe 同级目录**（可写目录落点）；
- ``resolve_resource_root()`` 返回 ``sys._MEIPASS``（只读资源落点）；
- ``sys._MEIPASS`` 缺失时优雅回退到 exe 同级目录。

以及 :mod:`app.utils.ffmpeg_utils` 的「包内 bin/ 优先于系统 PATH」解析顺序。

运行：``.venv/Scripts/python.exe -m unittest tests.test_frozen_compat -v``
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils import ffmpeg_utils
from app.utils.paths import resolve_project_root, resolve_resource_root


class TestFrozenPaths(unittest.TestCase):
    """冻结模式下项目根 / 资源根解析。"""

    def test_resolve_project_root_frozen_uses_exe_dir(self) -> None:
        exe = Path(tempfile.gettempdir()) / "StoryForge" / "StoryForge.exe"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", str(exe)
        ):
            self.assertEqual(resolve_project_root(), exe.resolve().parent)

    def test_resolve_resource_root_frozen_uses_meipass(self) -> None:
        exe = Path(tempfile.gettempdir()) / "StoryForge" / "StoryForge.exe"
        meipass = Path(tempfile.gettempdir()) / "StoryForge" / "_internal"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", str(exe)
        ), mock.patch.object(sys, "_MEIPASS", str(meipass), create=True):
            self.assertEqual(resolve_resource_root(), meipass)

    def test_resolve_resource_root_frozen_without_meipass_falls_back(self) -> None:
        exe = Path(tempfile.gettempdir()) / "StoryForge" / "StoryForge.exe"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", str(exe)
        ):
            if hasattr(sys, "_MEIPASS"):
                # 若环境已定义 _MEIPASS，临时删除以验证回退分支
                with mock.patch.object(sys, "_MEIPASS", "", create=True):
                    self.assertEqual(resolve_resource_root(), exe.resolve().parent)
            else:
                self.assertEqual(resolve_resource_root(), exe.resolve().parent)

    def test_resolve_project_root_not_frozen_is_repo_root(self) -> None:
        expected = Path(__file__).resolve().parents[1]
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(resolve_project_root(), expected)


class TestBundledBinPrecedence(unittest.TestCase):
    """包内 ``bin/`` 优先于系统 PATH。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bin_dir = Path(self._tmp.name) / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)

    def _write_fake(self, name: str) -> Path:
        target = self.bin_dir / name
        target.write_bytes(b"x")
        return target

    def test_bundled_ffmpeg_wins_over_path(self) -> None:
        target = self._write_fake("ffmpeg.exe")
        with mock.patch.object(ffmpeg_utils, "_bundled_bin_dir", return_value=self.bin_dir):
            resolved = ffmpeg_utils._resolve_bin("ffmpeg")
        self.assertEqual(resolved, str(target))

    def test_bundled_ffprobe_resolved(self) -> None:
        target = self._write_fake("ffprobe.exe")
        with mock.patch.object(ffmpeg_utils, "_bundled_bin_dir", return_value=self.bin_dir):
            self.assertEqual(ffmpeg_utils._ffprobe_bin(), str(target))

    def test_falls_back_to_path_when_no_bundle(self) -> None:
        with mock.patch.object(ffmpeg_utils, "_bundled_bin_dir", return_value=None):
            resolved = ffmpeg_utils._resolve_bin("python")
        self.assertTrue(resolved)  # 系统 PATH 中应能找到 python

    def test_require_ffmpeg_uses_bundled_binary(self) -> None:
        target = self._write_fake("ffmpeg.exe")
        with mock.patch.object(ffmpeg_utils, "_bundled_bin_dir", return_value=self.bin_dir):
            self.assertEqual(ffmpeg_utils._require_ffmpeg(), str(target))


if __name__ == "__main__":
    unittest.main(verbosity=2)
