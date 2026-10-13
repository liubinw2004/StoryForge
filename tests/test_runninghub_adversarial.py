"""对抗式独立验证：**视频 provider 体系替换为 RunningHub 工作流 i2v**。

本文件独立编写，**不复用**工程师的测试结论，目的是「证伪」而非「复述」。
仅使用本地桩服务（``http.server``），**绝不访问真实外网**（真实调用要花钱）。

覆盖方向：

1. **契约符合官方（P0）**——工作流四步端点 / ``nodeInfoList``（``fieldName=fieldValue``、
   image/prompt 字符串、duration/seed 整数）、上传取 ``data.fileName``、下载取 ``results[0].url``；
2. **成本安全（P1）**——``test_connection`` **绝不**打提交端点；源码级 ``httpx.post`` 调用点核对；
3. **错误码语义（P1）**——``errorCode`` 为 ``""`` / ``"0"`` / 缺失 / ``None`` 均不得误判失败；
4. **迁移不脏化（P1）**——旧 provider → mock，其它字段逐字段不变；
5. **零外网 / 删除彻底性 / 前端接线（P2）**。

运行（**必须禁代理**，避免本机代理把 127.0.0.1 连接被拒污染成 502）::

    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        .venv/Scripts/python.exe -m unittest tests.test_runninghub_adversarial -v
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.models.enums import Resolution, VideoProvider
from app.models.schemas import Config
from app.services import config_store as config_store_mod
from app.services.adapters import ADAPTER_REGISTRY, RunningHubAdapter, runninghub_adapter
from app.services.adapters.base import VideoError
from app.services.config_store import ConfigStore
from app.utils.paths import Paths
from app.utils.responses import ErrorCode

#: 已下线/未接入的视频 provider 残留 token（源码扫描用）。
_REMOVED_PROVIDER_TOKENS = ("kling", "seedance", "minimax", "generic", "hailuo")
#: RunningHub 工作流底层模型名（MiniMax H3 FL2VA）是**模型名**、非 provider；
#: 扫描前先剥离该合法别名，剥离后仍残留 provider 字样才判负。
_MODEL_NAME_ALIASES = ("minimax h3",)


def _strip_model_aliases(text: str) -> str:
    """剥离合法的 MiniMax-H3 模型名别名，供 provider 残留判定使用（大小写不敏感）。"""
    for alias in _MODEL_NAME_ALIASES:
        text = re.sub(re.escape(alias), "", text, flags=re.IGNORECASE)
    return text


_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_DIR = _REPO_ROOT / "app"
_ADAPTERS_DIR = _APP_DIR / "services" / "adapters"


# --------------------------------------------------------------------------- #
# 本地桩服务（独立实现，行为由 mode 驱动）
# --------------------------------------------------------------------------- #
class _StubHandler(BaseHTTPRequestHandler):
    """模拟 RunningHub 工作流 API：上传 → 提交 → 轮询 → 下载。"""

    protocol_version = "HTTP/1.1"

    mode = "ok"
    submit_paths: list[str] = []
    submit_bodies: list[dict] = []
    query_bodies: list[dict] = []
    query_calls = 0
    download_hits = 0
    upload_calls = 0

    def log_message(self, *args) -> None:  # noqa: D102 - 静默
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

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        port = self.server.server_address[1]
        if path.endswith("/query"):
            body = self._read_json()
            _StubHandler.query_calls += 1
            _StubHandler.query_bodies.append(body)
            mode = _StubHandler.mode
            if mode == "running_zero_code":
                self._send_json({"status": "RUNNING", "errorCode": "0", "errorMessage": ""})
            elif mode == "running_empty_code":
                self._send_json({"status": "RUNNING", "errorCode": "", "errorMessage": ""})
            elif mode == "running_missing_code":
                self._send_json({"status": "RUNNING"})
            elif mode == "running_none_code":
                self._send_json({"status": "RUNNING", "errorCode": None, "errorMessage": None})
            elif mode == "fail_business":
                self._send_json({"status": "RUNNING", "errorCode": "1001",
                                 "errorMessage": "quota exceeded"})
            else:
                if _StubHandler.query_calls < 2:
                    self._send_json({"status": "RUNNING", "errorCode": "", "errorMessage": ""})
                else:
                    self._send_json({"status": "SUCCESS", "errorCode": "", "errorMessage": "",
                                     "results": [{"url": f"http://127.0.0.1:{port}/dl/v.mp4"}]})
            return
        if path.endswith("/media/upload/binary"):
            _StubHandler.upload_calls += 1
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length:
                self.rfile.read(length)
            if _StubHandler.mode == "upload_fail":
                self._send_json({"code": 1, "msg": "upload rejected"})
            else:
                self._send_json({
                    "code": 0,
                    "data": {
                        "fileName": "openapi/frame.png",
                        "download_url": f"http://127.0.0.1:{port}/media/frame.png",
                        "size": 1234,
                        "type": "image/png",
                    },
                })
            return
        # 工作流提交端点
        body = self._read_json()
        _StubHandler.submit_paths.append(path)
        _StubHandler.submit_bodies.append(body)
        self._send_json({"taskId": "task-xyz", "status": "RUNNING",
                         "errorCode": "0", "errorMessage": ""})

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/dl/"):
            _StubHandler.download_hits += 1
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            payload = b"\x00\x00\x00\x18ftypmp42" + b"RHADV" * 32
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self._send_json({"error": "not found"}, status=404)


class _Stub:
    """可启停的桩服务。"""

    def __init__(self) -> None:
        _StubHandler.mode = "ok"
        _StubHandler.submit_paths = []
        _StubHandler.submit_bodies = []
        _StubHandler.query_bodies = []
        _StubHandler.query_calls = 0
        _StubHandler.download_hits = 0
        _StubHandler.upload_calls = 0
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def set_mode(self, mode: str) -> None:
        _StubHandler.mode = mode
        _StubHandler.query_calls = 0

    def start(self) -> "_Stub":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=3)


class _Case(unittest.TestCase):
    """共享：桩服务 + 临时目录 + 快速轮询。"""

    def setUp(self) -> None:
        self.stub = _Stub().start()
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_rhadv_"))
        self.out = str(self.tmp / "shots" / "segment_001.mp4")

    def tearDown(self) -> None:
        self.stub.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _adapter(self, *, api_key: str = "k", base_url: str | None = None) -> RunningHubAdapter:
        cfg = Config(
            video_provider=VideoProvider.RUNNINGHUB,
            video_api_key=api_key,
            video_base_url=(base_url if base_url is not None else self.stub.base_url),
        )
        return RunningHubAdapter(cfg)

    def _tiny_png(self) -> str:
        path = self.tmp / "frame.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"PNGDATA" * 8)
        return str(path)

    @contextmanager
    def _fast_poll(self, timeout: float = 5.0, interval: float = 0.01):
        with mock.patch.object(runninghub_adapter, "_POLL_INTERVAL_SEC", interval), \
                mock.patch.object(runninghub_adapter, "_POLL_TIMEOUT_SEC", timeout):
            yield


# --------------------------------------------------------------------------- #
# P0：契约符合官方（锁死端点 / 字段，防未来漂移）
# --------------------------------------------------------------------------- #
class TestContractConformance(_Case):
    """把工作流 i2v 契约锁为断言。"""

    def test_submit_endpoint_path_exact(self) -> None:
        adapter = self._adapter()
        with self._fast_poll():
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertEqual(
            _StubHandler.submit_paths[0],
            "/openapi/v2/run/workflow/2084067455902765057",
        )

    def test_endpoint_constants_match_workflow(self) -> None:
        self.assertEqual(
            runninghub_adapter.WORKFLOW_RUN_PATH_TEMPLATE,
            "/openapi/v2/run/workflow/{workflow_id}",
        )
        self.assertEqual(runninghub_adapter.UPLOAD_PATH, "/openapi/v2/media/upload/binary")
        self.assertEqual(runninghub_adapter.QUERY_PATH, "/openapi/v2/query")
        self.assertEqual(runninghub_adapter.DEFAULT_BASE_URL, "https://www.runninghub.cn")

    def test_node_info_list_uses_fieldvalue_not_value(self) -> None:
        adapter = self._adapter()
        nodes = adapter.build_node_info_list("p", 6, 42, "openapi/frame.png")
        self.assertEqual(len(nodes), 4)
        for node in nodes:
            self.assertIn("fieldValue", node, "字段名必须是 fieldValue")
            self.assertNotIn("value", node, "不得使用旧字段名 value")
        self.assertIsInstance(nodes[0]["fieldValue"], str)
        self.assertIsInstance(nodes[1]["fieldValue"], str)
        self.assertIsInstance(nodes[2]["fieldValue"], int)
        self.assertIsInstance(nodes[3]["fieldValue"], int)

    def test_upload_returns_file_name_not_url(self) -> None:
        adapter = self._adapter()
        file_name = adapter.upload_first_frame(self._tiny_png())
        self.assertEqual(file_name, "openapi/frame.png")
        self.assertEqual(_StubHandler.upload_calls, 1)

    def test_query_endpoint_and_body(self) -> None:
        adapter = self._adapter()
        with self._fast_poll():
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertEqual(_StubHandler.query_bodies[0], {"taskId": "task-xyz"})

    def test_download_from_results_url(self) -> None:
        adapter = self._adapter()
        with self._fast_poll():
            result = adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertEqual(result, self.out)
        self.assertTrue(Path(self.out).is_file())
        self.assertGreater(Path(self.out).stat().st_size, 0)
        self.assertEqual(_StubHandler.download_hits, 1)

    def test_auth_header_bearer(self) -> None:
        seen: dict[str, str] = {}
        real_post = runninghub_adapter.httpx.post

        def spy(url, *a, **k):  # noqa: ANN001
            seen.update(k.get("headers") or {})
            return real_post(url, *a, **k)

        adapter = self._adapter(api_key="rh-secret-123")
        with mock.patch.object(runninghub_adapter.httpx, "post", side_effect=spy), \
                self._fast_poll():
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertEqual(seen.get("Authorization"), "Bearer rh-secret-123")


# --------------------------------------------------------------------------- #
# P1：成本安全（探针绝不创建任务）
# --------------------------------------------------------------------------- #
class TestCostSafety(_Case):
    """test_connection 是零成本探针；只有 generate_i2v 会创建真实任务。"""

    def test_probe_does_not_hit_submit_endpoint(self) -> None:
        result = self._adapter().test_connection()
        self.assertTrue(result["ok"], result)
        self.assertEqual(_StubHandler.submit_bodies, [], "探针不得打提交端点（会花钱）")
        self.assertEqual(_StubHandler.submit_paths, [])
        self.assertEqual(_StubHandler.query_calls, 1)

    def test_probe_body_is_taskid_zero_only(self) -> None:
        self._adapter().test_connection()
        self.assertEqual(_StubHandler.query_bodies, [{"taskId": "0"}])

    def test_probe_without_key_makes_no_request(self) -> None:
        result = self._adapter(api_key="").test_connection()
        self.assertFalse(result["ok"])
        self.assertEqual(_StubHandler.query_calls, 0)
        self.assertEqual(_StubHandler.submit_bodies, [])

    def test_source_httpx_call_sites(self) -> None:
        """源码级核对：3 处 ``httpx.post``（探针 + 上传 + 通用 POST），1 处 ``httpx.stream``（下载）。"""
        src = Path("app/services/adapters/runninghub_adapter.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("httpx.post("), 3, "httpx.post 调用点数量异常")
        self.assertEqual(src.count("httpx.stream("), 1, "httpx.stream 调用点数量异常")


# --------------------------------------------------------------------------- #
# P1：errorCode 语义（不得误判）
# --------------------------------------------------------------------------- #
class TestErrorCodeSemantics(_Case):
    """空串 / "0" / 缺失 / None 均不算错误；非空才失败。"""

    def test_extract_error_matrix(self) -> None:
        ex = RunningHubAdapter._extract_error
        self.assertEqual(ex({}), ("", ""))
        self.assertEqual(ex({"errorCode": None, "errorMessage": None}), ("", ""))
        self.assertEqual(ex({"errorCode": "", "errorMessage": ""}), ("", ""))
        self.assertEqual(ex({"errorCode": 0}), ("0", ""))
        self.assertEqual(ex({"errorCode": "0"}), ("0", ""))
        self.assertEqual(ex({"errorCode": "  ", "errorMessage": "  "}), ("", ""))
        self.assertEqual(ex({"errorCode": "1001", "errorMessage": "quota"}), ("1001", "quota"))

    def test_running_with_zero_code_not_treated_as_failure(self) -> None:
        _StubHandler.mode = "running_zero_code"
        adapter = self._adapter()
        with self._fast_poll(timeout=0.08, interval=0.01), self.assertRaises(VideoError) as ctx:
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertIn("超时", str(ctx.exception))

    def test_running_missing_code_not_treated_as_failure(self) -> None:
        _StubHandler.mode = "running_missing_code"
        adapter = self._adapter()
        with self._fast_poll(timeout=0.08, interval=0.01), self.assertRaises(VideoError) as ctx:
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertIn("超时", str(ctx.exception))

    def test_running_none_code_not_treated_as_failure(self) -> None:
        _StubHandler.mode = "running_none_code"
        adapter = self._adapter()
        with self._fast_poll(timeout=0.08, interval=0.01), self.assertRaises(VideoError) as ctx:
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertIn("超时", str(ctx.exception))

    def test_running_empty_code_not_treated_as_failure(self) -> None:
        _StubHandler.mode = "running_empty_code"
        adapter = self._adapter()
        with self._fast_poll(timeout=0.08, interval=0.01), self.assertRaises(VideoError) as ctx:
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertIn("超时", str(ctx.exception))

    def test_nonempty_code_fails_immediately(self) -> None:
        _StubHandler.mode = "fail_business"
        adapter = self._adapter()
        with self._fast_poll(timeout=5.0, interval=0.01), self.assertRaises(VideoError) as ctx:
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
        self.assertEqual(ctx.exception.code, ErrorCode.VID_GENERATE_FAILED)
        self.assertIn("1001", str(ctx.exception))
        self.assertNotIn("超时", str(ctx.exception))


# --------------------------------------------------------------------------- #
# P1：迁移不脏化（team-lead 最担心的一条）
# --------------------------------------------------------------------------- #
class TestMigrationIntegrity(unittest.TestCase):
    """旧 provider → mock，且其它字段逐字段不变。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_mig_"))
        self.path = self.tmp / "config.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, payload: dict) -> None:
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_all_other_fields_preserved_on_legacy_provider(self) -> None:
        for legacy in _REMOVED_PROVIDER_TOKENS[:4]:
            with self.subTest(legacy=legacy):
                self._write({
                    "llm_mode": "openai",
                    "llm_base_url": "https://api.deepseek.com/v1",
                    "llm_api_key": "sk-abcdefghijklmnop-1234",
                    "llm_model": "deepseek-chat",
                    "video_provider": legacy,
                    "video_api_key": "rh-abcdefghijklmnop",
                    "video_base_url": "https://custom.example.com",
                    "video_model": "/openapi/v2/other/text-to-video",
                    "resolution": "1080p",
                    "style": "anime",
                    "shot_count": 7,
                    "shot_duration": 9,
                    "skip_video_eval": True,
                })
                cfg = ConfigStore(self.path).get_raw()
                self.assertEqual(cfg.video_provider.value, "mock")
                self.assertEqual(cfg.llm_mode.value, "openai")
                self.assertEqual(cfg.llm_base_url, "https://api.deepseek.com/v1")
                self.assertEqual(cfg.llm_api_key, "sk-abcdefghijklmnop-1234")
                self.assertEqual(cfg.llm_model, "deepseek-chat")
                self.assertEqual(cfg.video_api_key, "rh-abcdefghijklmnop")
                self.assertEqual(cfg.video_base_url, "https://custom.example.com")
                self.assertEqual(cfg.video_model, "/openapi/v2/other/text-to-video")
                self.assertEqual(cfg.resolution.value, "1080p")
                self.assertEqual(cfg.style, "anime")
                self.assertEqual(cfg.shot_count, 7)
                self.assertEqual(cfg.shot_duration, 9)
                self.assertTrue(cfg.skip_video_eval)
                self.assertNotEqual(cfg.llm_model, "qwen2.5:14b")
                self.assertNotEqual(cfg.shot_count, 4)

    def test_valid_providers_not_migrated(self) -> None:
        for keep in ("mock", "runninghub"):
            with self.subTest(keep=keep):
                self._write({"video_provider": keep, "shot_count": 5})
                cfg = ConfigStore(self.path).get_raw()
                self.assertEqual(cfg.video_provider.value, keep)
                self.assertEqual(cfg.shot_count, 5)


class TestMigrationHardening(unittest.TestCase):
    """P2-1 修复复核：**任意非法** video_provider → 回退 mock，其它字段逐字段不变。"""

    _OTHER = {
        "llm_mode": "openai",
        "llm_base_url": "https://api.deepseek.com/v1",
        "llm_api_key": "sk-abcdefghijklmnop-1234",
        "llm_model": "deepseek-chat",
        "video_api_key": "rh-abcdefghijklmnop",
        "video_base_url": "https://custom.example.com",
        "video_model": "/openapi/v2/other/text-to-video",
        "resolution": "1080p",
        "style": "anime",
        "shot_count": 7,
        "shot_duration": 9,
        "skip_video_eval": True,
    }

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_mighard_"))
        self.path = self.tmp / "config.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, payload: dict) -> Config:
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return ConfigStore(self.path).get_raw()

    def _assert_others_kept(self, cfg: Config) -> None:
        self.assertEqual(cfg.llm_mode.value, "openai")
        self.assertEqual(cfg.llm_base_url, "https://api.deepseek.com/v1")
        self.assertEqual(cfg.llm_api_key, "sk-abcdefghijklmnop-1234")
        self.assertEqual(cfg.llm_model, "deepseek-chat")
        self.assertEqual(cfg.video_api_key, "rh-abcdefghijklmnop")
        self.assertEqual(cfg.video_base_url, "https://custom.example.com")
        self.assertEqual(cfg.video_model, "/openapi/v2/other/text-to-video")
        self.assertEqual(cfg.resolution.value, "1080p")
        self.assertEqual(cfg.style, "anime")
        self.assertEqual(cfg.shot_count, 7)
        self.assertEqual(cfg.shot_duration, 9)
        self.assertTrue(cfg.skip_video_eval)
        self.assertNotEqual(cfg.llm_model, "qwen2.5:14b")
        self.assertNotEqual(cfg.shot_count, 4)
        self.assertNotEqual(cfg.video_api_key, "")

    def test_invalid_providers_matrix_all_fall_back_and_keep_others(self) -> None:
        bad_values = (
            "kling", "seedance", "minimax", "generic",
            "foo", "runninbghub",
            "RUNNINGHUB", "mock ",
            123, 4.5, True, [], {"a": 1},
            None,
        )
        for bad in bad_values:
            with self.subTest(provider=bad):
                cfg = self._load({**self._OTHER, "video_provider": bad})
                self.assertEqual(cfg.video_provider.value, "mock", f"{bad!r} 应回退 mock")
                self._assert_others_kept(cfg)

    def test_missing_key_defaults_mock_others_kept(self) -> None:
        cfg = self._load(dict(self._OTHER))
        self.assertEqual(cfg.video_provider.value, "mock")
        self._assert_others_kept(cfg)

    def test_valid_providers_not_migrated_others_kept(self) -> None:
        for keep in ("mock", "runninghub"):
            with self.subTest(provider=keep):
                cfg = self._load({**self._OTHER, "video_provider": keep})
                self.assertEqual(cfg.video_provider.value, keep)
                self._assert_others_kept(cfg)

    def test_null_plus_other_custom_fields_kept(self) -> None:
        cfg = self._load({**self._OTHER, "video_provider": None})
        self.assertEqual(cfg.video_provider.value, "mock")
        self._assert_others_kept(cfg)


# --------------------------------------------------------------------------- #
# P2：零外网
# --------------------------------------------------------------------------- #
class TestNoExternalNetwork(_Case):
    """全程只与 127.0.0.1 通信，任何外部主机都要报错。"""

    @contextmanager
    def _guard_network(self):
        hosts: list[str] = []
        real_post = runninghub_adapter.httpx.post
        real_stream = runninghub_adapter.httpx.stream

        def _check(url) -> None:  # noqa: ANN001
            host = urllib.parse.urlparse(str(url)).hostname or ""
            hosts.append(host)
            self.assertIn(host, ("127.0.0.1", "localhost"), f"检测到外部网络请求：{host}")

        def gpost(url, *a, **k):  # noqa: ANN001
            _check(url)
            return real_post(url, *a, **k)

        def gstream(method, url, *a, **k):  # noqa: ANN001
            _check(url)
            return real_stream(method, url, *a, **k)

        with mock.patch.object(runninghub_adapter.httpx, "post", side_effect=gpost), \
                mock.patch.object(runninghub_adapter.httpx, "stream", side_effect=gstream):
            yield hosts

    def test_generate_and_probe_only_loopback(self) -> None:
        adapter = self._adapter()
        with self._guard_network() as hosts, self._fast_poll():
            adapter.generate_i2v(self._tiny_png(), "p", 6, 42, "1280x720", self.out)
            adapter.test_connection()
        self.assertTrue(hosts, "应至少捕获到请求")
        self.assertTrue(all(h in ("127.0.0.1", "localhost") for h in hosts))


# --------------------------------------------------------------------------- #
# P2：删除彻底性
# --------------------------------------------------------------------------- #
class TestDeletionThoroughness(unittest.TestCase):
    """已下线 provider 与自建图像组件应被彻底删除，源码无残留。"""

    def test_enum_only_mock_and_runninghub(self) -> None:
        self.assertEqual({p.value for p in VideoProvider}, {"mock", "runninghub"})

    def test_registry_only_mock_and_runninghub(self) -> None:
        self.assertEqual(set(ADAPTER_REGISTRY), {VideoProvider.MOCK, VideoProvider.RUNNINGHUB})

    def test_removed_adapter_modules_do_not_import(self) -> None:
        for mod in ("kling", "seedance", "minimax", "generic", "image_base", "image_mock_adapter",
                    "image_local_adapter", "image_runninghub_adapter"):
            with self.subTest(module=mod):
                with self.assertRaises(ModuleNotFoundError):
                    importlib.import_module(f"app.services.adapters.{mod}_adapter")

    def test_adapters_dir_has_no_removed_files(self) -> None:
        files = {p.name for p in _ADAPTERS_DIR.glob("*.py")}
        for token in ("kling", "seedance", "minimax", "generic", "image_"):
            self.assertFalse(
                any(f.startswith(token) for f in files), f"adapters 残留 {token} 文件：{files}"
            )

    def test_app_source_has_no_provider_residue(self) -> None:
        """app/ 源码不得残留已下线 provider 字样（先剥离合法模型名别名）。"""
        hits: list[str] = []
        for py in _APP_DIR.rglob("*.py"):
            text = py.read_text(encoding="utf-8", errors="ignore")
            low = _strip_model_aliases(text.lower())
            if any(tok in low for tok in _REMOVED_PROVIDER_TOKENS):
                rel = str(py).replace("\\", "/")
                if rel.endswith("app/services/config_store.py"):
                    continue
                hits.append(rel)
        self.assertEqual(hits, [], f"app/ 源码残留已下线 provider：{hits}")

    def test_static_assets_have_no_provider_residue(self) -> None:
        for name in ("index.html", "app.js"):
            text = (Paths.STATIC_DIR / name).read_text(encoding="utf-8").lower()
            for tok in _REMOVED_PROVIDER_TOKENS:
                self.assertNotIn(tok, text, f"{name} 残留 {tok}")


# --------------------------------------------------------------------------- #
# P2：前端接线
# --------------------------------------------------------------------------- #
class TestFrontendWiring(unittest.TestCase):
    """静态前端只暴露 Mock / RunningHub 两个选项，rh_* 输入完整，无图像配置残留。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (Paths.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        cls.js = (Paths.STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def test_video_provider_radios_exactly_two(self) -> None:
        values = re.findall(r'name="video_provider"\s+value="([^"]+)"', self.html)
        self.assertEqual(set(values), {"mock", "runninghub"}, f"radio 取值异常：{values}")
        self.assertEqual(len(values), 2)

    def test_default_checked_is_mock(self) -> None:
        self.assertRegex(self.html, r'name="video_provider"\s+value="mock"\s+checked')

    def test_js_dollar_ids_all_exist(self) -> None:
        html_ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        refs = sorted(set(re.findall(r"""\$\(\s*["']([^"']+)["']\s*\)""", self.js)))
        missing = [r for r in refs if r not in html_ids]
        self.assertEqual(missing, [], f"app.js 引用了不存在的 id：{missing}")

    def test_js_reads_provider_radio_and_toggles(self) -> None:
        self.assertIn('selectedRadio("video_provider")', self.js)
        self.assertRegex(self.js, r'provider\s*===\s*"mock"')

    def test_rh_config_inputs_exist(self) -> None:
        html_ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        rh_ids = {
            "rhWorkflowId", "rhInstanceType", "rhRetainSeconds",
            "rhFirstFrameNodeId", "rhFirstFrameFieldName",
            "rhPromptNodeId", "rhPromptFieldName",
            "rhDurationNodeId", "rhDurationFieldName",
            "rhSeedNodeId", "rhSeedFieldName",
        }
        self.assertEqual(rh_ids - html_ids, set(), f"index.html 缺少 rh_* 配置：{rh_ids - html_ids}")

    def test_no_image_config_ids(self) -> None:
        html_ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        removed = {
            "imageEndpoint", "imageApiKey", "ipScale", "ipFace", "imageSeed",
            "shotFirstFrame", "autoEngineSwitch", "imageSwitchStartCmd",
            "imageSwitchStopCmd", "testImageBtn",
        }
        self.assertEqual(removed & html_ids, set(), f"index.html 残留图像配置：{removed & html_ids}")

    def test_chain_ids_exist(self) -> None:
        html_ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        chain_ids = {
            "section-chain", "chainChip", "chainHint", "chainStartArea",
            "chainFileInput", "chainStartBtn", "chainSegments",
        }
        self.assertEqual(chain_ids - html_ids, set(), f"index.html 缺少逐段确认区：{chain_ids - html_ids}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
