"""单元测试：``app.utils.paths.Paths.safe_join`` 路径穿越校验。

运行：``.venv/Scripts/python.exe -m unittest tests.test_paths_security -v``
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.utils.paths import Paths


class TestSafeJoin(unittest.TestCase):
    """safe_join 安全边界。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_test_paths_"))
        (self.tmp / "shots").mkdir(parents=True, exist_ok=True)
        (self.tmp / "final_video.mp4").write_text("x", encoding="utf-8")
        (self.tmp / "shots" / "shot_001.mp4").write_text("y", encoding="utf-8")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_allow_normal_relative(self) -> None:
        target = Paths.safe_join(self.tmp, "shots/shot_001.mp4")
        self.assertEqual(target.name, "shot_001.mp4")
        self.assertTrue(str(target).startswith(str(self.tmp.resolve())))

    def test_reject_dotdot(self) -> None:
        for bad in ("../secret.txt", "..\\secret.txt", "shots/../..\\secret.txt", "../../etc/passwd"):
            with self.assertRaises(ValueError) as ctx:
                Paths.safe_join(self.tmp, bad)
            self.assertIn("路径穿越", str(ctx.exception))

    def test_reject_absolute_path(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Paths.safe_join(self.tmp, "C:\\Windows\\win.ini")
        self.assertIn("路径穿越", str(ctx.exception))

    def test_reject_empty(self) -> None:
        with self.assertRaises(ValueError):
            Paths.safe_join(self.tmp, "")

    def test_reject_null_byte(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Paths.safe_join(self.tmp, "a\x00b")
        self.assertIn("空字节", str(ctx.exception))

    def test_percent_encoded_is_literal_here(self) -> None:
        """注意：函数层不负责 URL 解码。

        字面量 ``%2e%2e%2f`` 不含 ``/``，会被当作普通文件名，**仍位于 base 之内**
        （不会越权）。真正的解码发生在 HTTP 路由层，需由 E2E 测试覆盖
        （FastAPI 会把 ``%2e%2e%2f`` 解码为 ``../`` 再交给 safe_join 并拒绝）。
        """
        target = Paths.safe_join(self.tmp, "%2e%2e%2fconfig.json")
        self.assertTrue(str(target.resolve()).startswith(str(self.tmp.resolve())))

    def test_to_rel_outputs(self) -> None:
        inside = Paths.OUTPUTS_DIR / "20260101_000000" / "script.json"
        rel = Paths.to_rel_outputs(inside)
        self.assertTrue(rel.startswith("outputs/"))
        self.assertIn("script.json", rel)
        # 不在 outputs 内时退化为文件名
        self.assertEqual(Paths.to_rel_outputs(Path("E:/tmp/foo.json")), "foo.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
