"""RunningHub 工作流 i2v 适配器单元测试（**绝不访问真实外网**）。

用一个本地桩 HTTP 服务（``http.server``）模拟 RunningHub **工作流 API** 的四步异步流程：

  1) ``POST {base}/openapi/v2/media/upload/binary``       上传首帧 → ``{"data": {"fileName": ...}}``
  2) ``POST {base}/openapi/v2/run/workflow/{workflow_id}`` 提交 → ``{"taskId": ...}``
  3) ``POST {base}/openapi/v2/query``                      轮询 → ``{"status": "SUCCESS", "results": [...]}``
  4) ``GET  results[0].url``                               下载 mp4 落盘

覆盖：四步落盘、``nodeInfoList``（``fieldName=fieldValue``、image/prompt 字符串、duration/seed 整数）、
上传取 ``data.fileName``（非 URL）、提交体（``nodeInfoList``+``instanceType``+``retainSeconds``）、
鉴权失败、``status=FAILED``（含 805）、RUNNING+非空 errorCode（业务失败）、轮询超时、
未配 Key、``test_connection`` 分支、注册表 / 枚举。

运行（**必须禁代理**，避免本地代理拦截 127.0.0.1）：
``env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY .venv/Scripts/python.exe -m unittest tests.test_runninghub_adapter -v``
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.models.enums import VideoProvider
from app.models.schemas import Config
from app.services.adapters import ADAPTER_REGISTRY, RunningHubAdapter, runninghub_adapter
from app.services.adapters.base import VideoError
from app.utils.responses import ErrorCode


# --------------------------------------------------------------------------- #
# 本地桩服务（工作流 API）
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    """RunningHub 工作流 API 桩处理器；行为由类属性 ``mode`` 决定。"""

    protocol_version = "HTTP/1.1"

    mode = "ok"                 # ok | failed | business_fail | running | submit_auth | query_auth | upload_fail | missing_file_name
    submit_bodies: list[dict] = []
    submit_paths: list[str] = []
    query_bodies: list[dict] = []
    query_calls = 0
    download_hits = 0
    upload_calls = 0
    upload_bodies: list[dict] = []

    def log_message(self, *args) -> None:  # noqa: D102 - 静默日志
        pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, ctype: str = "video/mp4") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        path = urllib.parse.urlparse(self.path).path
        port = self.server.server_address[1]
        if path.endswith("/query"):
            body = self._read_json()
            _Handler.query_bodies.append(body)
            _Handler.query_calls += 1
            if _Handler.mode == "query_auth":
                self._send_json(
                    {"errorCode": "433", "errorMessage": "API key invalid or unauthorized"}
                )
            elif _Handler.mode == "failed":
                self._send_json(
                    {"status": "FAILED", "errorCode": "805", "errorMessage": "task failed",
                     "results": None}
                )
            elif _Handler.mode == "business_fail":
                self._send_json(
                    {"status": "RUNNING", "errorCode": "1001", "errorMessage": "quota exceeded",
                     "results": None}
                )
            elif _Handler.mode == "running":
                self._send_json({"status": "RUNNING", "errorCode": "", "errorMessage": ""})
            else:  # ok：首次 RUNNING，其后 SUCCESS（带 results[0].url）
                if _Handler.query_calls < 2:
                    self._send_json({"status": "RUNNING", "errorCode": "", "errorMessage": ""})
                else:
                    self._send_json(
                        {"status": "SUCCESS", "errorCode": "", "errorMessage": "",
                         "results": [{"url": f"http://127.0.0.1:{port}/dl/v.mp4"}]}
                    )
            return
        if path.endswith("/media/upload/binary"):
            _Handler.upload_calls += 1
            length = int(self.headers.get("Content-Length", "0") or "0")
            _Handler.upload_bodies.append({"content_length": length,
                                           "content_type": self.headers.get("Content-Type", "")})
            if length:
                self.rfile.read(length)
            if _Handler.mode == "upload_fail":
                self._send_json({"code": 1, "msg": "upload rejected"})
            elif _Handler.mode == "missing_file_name":
                self._send_json({"code": 0, "data": {"download_url": "http://x/y"}})
            else:
                self._send_json({
                    "code": 0,
                    "data": {
                        "fileName": "openapi/frame.png",
                        "download_url": f"http://127.0.0.1:{port}/media/frame.png",
                        "size": length,
                        "type": "image/png",
                    },
                })
            return
        # 工作流提交端点
        body = self._read_json()
        _Handler.submit_bodies.append(body)
        _Handler.submit_paths.append(path)
        if _Handler.mode == "submit_auth":
            self._send_json(
                {"taskId": "", "status": "FAILED", "errorCode": "433",
                 "errorMessage": "invalid api key / insufficient permission"}
            )
        else:
            self._send_json(
                {"taskId": "task-abc", "status": "RUNNING", "errorCode": "", "errorMessage": "",
                 "results": None}
            )

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/dl/"):
            _Handler.download_hits += 1
            self._send_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"RUNNINGHUB" * 16)
        else:
            self._send_json({"error": "not found"}, status=404)


class RunningHubStub:
    """可启停的 RunningHub 工作流 API 桩服务封装。"""

    def __init__(self, port: int = 0) -> None:
        """在 127.0.0.1 上创建桩服务（port=0 自动分配）。"""
        _Handler.mode = "ok"
        _Handler.submit_bodies = []
        _Handler.submit_paths = []
        _Handler.query_bodies = []
        _Handler.query_calls = 0
        _Handler.download_hits = 0
        _Handler.upload_calls = 0
        _Handler.upload_bodies = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def set_mode(self, mode: str) -> None:
        _Handler.mode = mode
        _Handler.query_calls = 0

    @property
    def submitted_bodies(self) -> list[dict]:
        return _Handler.submit_bodies

    @property
    def submit_paths(self) -> list[str]:
        return _Handler.submit_paths

    @property
    def query_bodies(self) -> list[dict]:
        return _Handler.query_bodies

    @property
    def query_calls(self) -> int:
        return _Handler.query_calls

    @property
    def download_hits(self) -> int:
        return _Handler.download_hits

    @property
    def upload_calls(self) -> int:
        return _Handler.upload_calls

    def start(self) -> "RunningHubStub":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=3)


# --------------------------------------------------------------------------- #
# 基类
# --------------------------------------------------------------------------- #
class _AdapterCase(unittest.TestCase):
    """共享：本地桩 + 临时输出目录。"""

    def setUp(self) -> None:
        self.stub = RunningHubStub(0).start()
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_rh_"))
        self.out = str(self.tmp / "shots" / "segment_001.mp4")

    def tearDown(self) -> None:
        self.stub.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _adapter(
        self,
        *,
        api_key: str = "test-key",
        base_url: str | None = None,
        workflow_id: str = "2084067455902765057",
    ) -> RunningHubAdapter:
        cfg = Config(
            video_provider=VideoProvider.RUNNINGHUB,
            video_api_key=api_key,
            video_base_url=(base_url if base_url is not None else self.stub.base_url),
            rh_workflow_id=workflow_id,
        )
        return RunningHubAdapter(cfg)

    def _tiny_png(self, name: str = "frame.png") -> str:
        path = self.tmp / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"PNGDATA" * 8)
        return str(path)

    @contextmanager
    def _fast_poll(self, timeout: float = 5.0, interval: float = 0.01):
        with mock.patch.object(runninghub_adapter, "_POLL_INTERVAL_SEC", interval), mock.patch.object(
            runninghub_adapter, "_POLL_TIMEOUT_SEC", timeout
        ):
            yield


# --------------------------------------------------------------------------- #
# 正常链路 / 契约 / 规格
# --------------------------------------------------------------------------- #
class TestRunningHubHappyPath(_AdapterCase):
    """工作流四步异步链路与 nodeInfoList 契约。"""

    def test_full_four_step_pipeline_writes_file(self) -> None:
        self.stub.set_mode("ok")
        adapter = self._adapter()
        frame = self._tiny_png()
        with self._fast_poll():
            result = adapter.generate_i2v(frame, "一只猫在弹钢琴", 6, 42, "1280x720", self.out)

        self.assertEqual(result, self.out)
        self.assertTrue(Path(self.out).is_file(), "未落盘视频文件")
        self.assertGreater(Path(self.out).stat().st_size, 0, "落盘文件为空")
        self.assertEqual(self.stub.upload_calls, 1, "应上传一次首帧")
        self.assertEqual(self.stub.download_hits, 1)
        self.assertGreaterEqual(self.stub.query_calls, 2, "应至少轮询两次（RUNNING → SUCCESS）")

    def test_submit_body_is_node_info_list_plus_instance_type(self) -> None:
        self.stub.set_mode("ok")
        adapter = self._adapter()
        with self._fast_poll():
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)

        body = self.stub.submitted_bodies[0]
        self.assertEqual(set(body.keys()), {"nodeInfoList", "instanceType", "usePersonalQueue", "retainSeconds"})
        self.assertEqual(body["instanceType"], "default")
        self.assertIs(body["usePersonalQueue"], False)
        self.assertEqual(body["retainSeconds"], 180)

        nodes = body["nodeInfoList"]
        self.assertEqual(len(nodes), 4)
        for node in nodes:
            self.assertIn("fieldValue", node, "字段名必须是 fieldValue（不是 value）")
            self.assertNotIn("value", node)
        # image/prompt 字符串；duration/seed 整数
        self.assertEqual(nodes[0]["fieldValue"], "openapi/frame.png")
        self.assertIsInstance(nodes[0]["fieldValue"], str)
        self.assertEqual(nodes[1]["fieldValue"], "p")
        self.assertIsInstance(nodes[1]["fieldValue"], str)
        self.assertEqual(nodes[2]["fieldValue"], 6)
        self.assertIsInstance(nodes[2]["fieldValue"], int)
        self.assertEqual(nodes[3]["fieldValue"], 42)
        self.assertIsInstance(nodes[3]["fieldValue"], int)

    def test_submit_path_contains_workflow_id(self) -> None:
        self.stub.set_mode("ok")
        adapter = self._adapter(workflow_id="wf-custom-1")
        with self._fast_poll():
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertEqual(self.stub.submit_paths[0], "/openapi/v2/run/workflow/wf-custom-1")

    def test_query_body_carries_taskid(self) -> None:
        self.stub.set_mode("ok")
        adapter = self._adapter()
        with self._fast_poll():
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertEqual(self.stub.query_bodies[0], {"taskId": "task-abc"})

    def test_upload_first_frame_returns_file_name_not_url(self) -> None:
        self.stub.set_mode("ok")
        adapter = self._adapter()
        file_name = adapter.upload_first_frame(self._tiny_png())
        self.assertEqual(file_name, "openapi/frame.png", "必须取 data.fileName（非 URL）")

    def test_build_node_info_list_order_and_defaults(self) -> None:
        adapter = self._adapter()
        nodes = adapter.build_node_info_list("prompt text", 4, 7, "openapi/f.png")
        self.assertEqual([n["nodeId"] for n in nodes], ["4", "7", "6", "9"])
        self.assertEqual([n["fieldName"] for n in nodes],
                         ["image", "prompt", "duration_seconds", "seed"])


# --------------------------------------------------------------------------- #
# 错误路径
# --------------------------------------------------------------------------- #
class TestRunningHubErrors(_AdapterCase):
    """各类失败的正确错误码与可读中文信息。"""

    def test_upload_missing_file_name_raises_api_error(self) -> None:
        self.stub.set_mode("missing_file_name")
        adapter = self._adapter()
        with self.assertRaises(VideoError) as ctx:
            adapter.upload_first_frame(self._tiny_png())
        self.assertEqual(ctx.exception.code, ErrorCode.VID_API_ERROR)
        self.assertIn("fileName", str(ctx.exception))

    def test_upload_fail_raises_api_error(self) -> None:
        self.stub.set_mode("upload_fail")
        adapter = self._adapter()
        with self.assertRaises(VideoError) as ctx:
            adapter.upload_first_frame(self._tiny_png())
        self.assertEqual(ctx.exception.code, ErrorCode.VID_API_ERROR)

    def test_submit_auth_error_raises_api_error(self) -> None:
        self.stub.set_mode("submit_auth")
        adapter = self._adapter()
        with self.assertRaises(VideoError) as ctx:
            adapter.submit_workflow(adapter.build_node_info_list("p", 6, 1, "f.png"), "default", 180)
        self.assertEqual(ctx.exception.code, ErrorCode.VID_API_ERROR)
        self.assertIn("433", str(ctx.exception))

    def test_status_failed_raises_generate_failed_with_805(self) -> None:
        self.stub.set_mode("failed")
        adapter = self._adapter()
        with self._fast_poll(), self.assertRaises(VideoError) as ctx:
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertEqual(ctx.exception.code, ErrorCode.VID_GENERATE_FAILED)
        self.assertIn("805", str(ctx.exception))
        self.assertEqual(self.stub.download_hits, 0)

    def test_running_with_nonempty_errorcode_is_business_failure(self) -> None:
        """非空 errorCode = 业务失败（即便 status 仍为 RUNNING），应立即失败而非空等超时。"""
        self.stub.set_mode("business_fail")
        adapter = self._adapter()
        with self._fast_poll(timeout=0.25, interval=0.02), self.assertRaises(VideoError) as ctx:
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertEqual(ctx.exception.code, ErrorCode.VID_GENERATE_FAILED)
        self.assertIn("1001", str(ctx.exception))
        self.assertNotIn("超时", str(ctx.exception))

    def test_poll_timeout_raises_generate_failed(self) -> None:
        self.stub.set_mode("running")
        adapter = self._adapter()
        with self._fast_poll(timeout=0.05, interval=0.01), self.assertRaises(VideoError) as ctx:
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertEqual(ctx.exception.code, ErrorCode.VID_GENERATE_FAILED)
        self.assertIn("超时", str(ctx.exception))

    def test_missing_api_key_raises_not_configured(self) -> None:
        adapter = self._adapter(api_key="")
        with self.assertRaises(VideoError) as ctx:
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertEqual(ctx.exception.code, ErrorCode.VID_NOT_CONFIGURED)
        self.assertIn("API Key", str(ctx.exception))
        # 未配置时不得发起任何网络请求
        self.assertEqual(self.stub.submitted_bodies, [])
        self.assertEqual(self.stub.upload_calls, 0)

    def test_missing_workflow_id_raises_not_configured(self) -> None:
        adapter = self._adapter(workflow_id="")
        with self.assertRaises(VideoError) as ctx:
            adapter.submit_workflow([], "default", 180)
        self.assertEqual(ctx.exception.code, ErrorCode.VID_NOT_CONFIGURED)

    def test_invalid_out_path_raises_invalid_param(self) -> None:
        adapter = self._adapter()
        with self.assertRaises(VideoError) as ctx:
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", "")
        self.assertEqual(ctx.exception.code, ErrorCode.VID_INVALID_PARAM)

    def test_generate_without_image_ref_raises_invalid_param(self) -> None:
        """旧 ``generate()`` 入口：工作流 i2v 必须有首帧图。"""
        adapter = self._adapter()
        with self.assertRaises(VideoError) as ctx:
            adapter.generate("p", None, 6, "1280x720", "realistic", self.out)
        self.assertEqual(ctx.exception.code, ErrorCode.VID_INVALID_PARAM)


# --------------------------------------------------------------------------- #
# test_connection 四分支
# --------------------------------------------------------------------------- #
class TestRunningHubTestConnection(_AdapterCase):
    """零成本探针：不创建真实任务。"""

    def test_missing_key(self) -> None:
        result = self._adapter(api_key="").test_connection()
        self.assertFalse(result["ok"])
        self.assertEqual(result["provider"], "runninghub")
        self.assertIn("未配置", result["message"])

    def test_auth_failed(self) -> None:
        self.stub.set_mode("query_auth")
        result = self._adapter().test_connection()
        self.assertFalse(result["ok"])
        self.assertIn("无效或未授权", result["message"])

    def test_structured_response_means_ok(self) -> None:
        self.stub.set_mode("ok")
        result = self._adapter().test_connection()
        self.assertTrue(result["ok"])
        # 探针不得创建真实任务（提交体为空），但会打一次查询
        self.assertEqual(self.stub.submitted_bodies, [])
        self.assertEqual(self.stub.query_calls, 1)

    def test_unreachable(self) -> None:
        result = self._adapter(base_url="http://127.0.0.1:1").test_connection()
        self.assertFalse(result["ok"])


# --------------------------------------------------------------------------- #
# 注册表 / 枚举
# --------------------------------------------------------------------------- #
class TestRunningHubRegistration(unittest.TestCase):
    """适配器注册与枚举契约。"""

    def test_registry_contains_runninghub(self) -> None:
        self.assertIs(ADAPTER_REGISTRY[VideoProvider.RUNNINGHUB], RunningHubAdapter)

    def test_registry_only_mock_and_runninghub(self) -> None:
        self.assertEqual(set(ADAPTER_REGISTRY.keys()), {VideoProvider.MOCK, VideoProvider.RUNNINGHUB})

    def test_removed_providers_are_gone(self) -> None:
        for name in ("kling", "seedance", "minimax", "generic"):
            self.assertNotIn(name, {p.value for p in VideoProvider})

    def test_enum_value_and_label(self) -> None:
        self.assertEqual(VideoProvider.RUNNINGHUB.value, "runninghub")
        self.assertIn("工作流 i2v", VideoProvider.RUNNINGHUB.label)


# --------------------------------------------------------------------------- #
# 端点解析（回归：base_url 自带路径时**绝不产生双路径**）
# --------------------------------------------------------------------------- #
class TestEndpointResolution(_AdapterCase):
    """base_url 应被拆为纯 origin + path；工作流提交只用 origin 拼固定路径。"""

    def test_split_url_variants(self) -> None:
        cases = {
            "https://www.runninghub.cn": ("https://www.runninghub.cn", ""),
            "https://www.runninghub.cn/": ("https://www.runninghub.cn", ""),
            "www.runninghub.cn": ("https://www.runninghub.cn", ""),  # 裸主机补 https
            "https://www.runninghub.cn/some/path/": (
                "https://www.runninghub.cn", "/some/path",
            ),  # 尾斜杠被规范化
            "": (runninghub_adapter.DEFAULT_BASE_URL, ""),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(RunningHubAdapter._split_url(raw), expected)

    def test_base_url_with_path_does_not_double_workflow_path(self) -> None:
        """即便 base_url 自带路径，工作流提交 URL 也必须只含一段 /openapi/v2/。"""
        adapter = self._adapter(base_url=f"{self.stub.base_url}/openapi/v2/run/workflow")
        self.assertEqual(adapter.base_url, self.stub.base_url)
        nodes = adapter.build_node_info_list("p", 6, 1, "f.png")
        with mock.patch.object(
            runninghub_adapter.httpx,
            "post",
            return_value=type("Resp", (), {
                "status_code": 200,
                "json": lambda self: {"taskId": "t", "status": "RUNNING", "errorCode": "", "errorMessage": ""},
            })(),
        ) as post:
            adapter.submit_workflow(nodes, "default", 180)
        url = post.call_args[0][0]
        self.assertEqual(url.count("/openapi/v2/"), 1, f"工作流提交出现双路径：{url}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
