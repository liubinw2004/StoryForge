"""``app/main.py`` 端口探测测试（修复 F3）。

核心断言：**已被占用的端口必须被识别为「占用」并跳过**。旧实现给探测 socket 设了
``SO_REUSEADDR``，在 Windows 上会「绑上已被占用的端口」从而误判为空闲，导致第二个
实例仍选 8000。
"""

from __future__ import annotations

import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import find_free_port

HOST = "127.0.0.1"


def _find_base_port(start: int = 49500, end: int = 49900) -> int:
    """找一个当前空闲的基准端口。"""
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError("无法找到空闲基准端口")


class TestFindFreePort(unittest.TestCase):
    """端口探测行为（F3）。"""

    def _occupy(self, port: int) -> socket.socket:
        """占用并监听一个端口（模拟另一个已启动的服务实例）。"""
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(holder.close)
        holder.bind((HOST, port))
        holder.listen(1)
        return holder

    def test_returns_start_when_free(self):
        base = _find_base_port()
        self.assertEqual(find_free_port(base, HOST), base)

    def test_occupied_port_is_skipped(self):
        """F3 核心：占用的端口不得被误判为空闲。"""
        base = _find_base_port()
        self._occupy(base)
        got = find_free_port(base, HOST)
        self.assertNotEqual(got, base, "被占用的端口被误判为空闲（SO_REUSEADDR 缺陷）")
        self.assertGreater(got, base)

    def test_occupied_port_with_reuseaddr_is_also_skipped(self):
        """占用方自身设置了 SO_REUSEADDR（uvicorn 常见情况）时同样必须跳过。"""
        base = _find_base_port()
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(holder.close)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind((HOST, base))
        holder.listen(1)

        got = find_free_port(base, HOST)
        self.assertNotEqual(got, base)
        self.assertGreater(got, base)

    def test_returned_port_is_actually_bindable(self):
        base = _find_base_port()
        got = find_free_port(base, HOST)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((HOST, got))

    def test_skips_multiple_consecutive_occupied_ports(self):
        base = _find_base_port()
        for offset in range(3):
            self._occupy(base + offset)
        got = find_free_port(base, HOST)
        self.assertGreaterEqual(got, base + 3)
        self.assertNotIn(got, (base, base + 1, base + 2))

    def test_raises_when_no_port_available(self):
        base = _find_base_port()
        self._occupy(base)
        self._occupy(base + 1)
        with self.assertRaises(RuntimeError):
            find_free_port(base, HOST, tries=2)

    def test_default_start_is_8000(self):
        """常量回归：默认起始端口仍为 8000。"""
        import app.main as main_module

        self.assertEqual(main_module.DEFAULT_PORT, 8000)


if __name__ == "__main__":
    unittest.main()
