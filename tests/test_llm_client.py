"""单元测试：``app.services.llm_client`` 的 OpenAI / Ollama 双模式与错误分支。

用标准库 ``http.server`` 起桩服务，**不联网、不依赖真实 Ollama**。
运行：``.venv/Scripts/python.exe -m unittest tests.test_llm_client -v``
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.models.enums import LLMMode
from app.models.schemas import Config
from app.services.llm_client import (
    DEFAULT_MAX_TOKENS,
    LLMClient,
    LLMError,
    OllamaClient,
    OpenAICompatClient,
    build_llm_client,
)
from app.utils.responses import ErrorCode
from tests.stub_llm_server import STUB_MODEL, start_stub_server


# --------------------------------------------------------------------------- #
# 专用错误桩：401 / 空内容 / 成功
# --------------------------------------------------------------------------- #
def _make_server(handler_cls) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


class _UnauthorizedHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: D102
        pass

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._reply(401, {"error": {"message": "Invalid API key", "type": "invalid_request_error"}})

    def do_POST(self) -> None:  # noqa: N802
        self._reply(401, {"error": {"message": "Invalid API key", "type": "invalid_request_error"}})


class _EmptyContentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: D102
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        self.rfile.read(length)
        body = json.dumps(
            {"choices": [{"index": 0, "message": {"role": "assistant", "content": "   "}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"object": "list", "data": [{"id": STUB_MODEL}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ThinkingOnlyHandler(BaseHTTPRequestHandler):
    """返回空 ``content`` + 非空 ``reasoning``，模拟思考模式耗尽额度。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: D102
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        self.rfile.read(length)
        body = json.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning": "我在思考该怎么回答……",
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestOpenAICompatClient(unittest.TestCase):
    """OpenAI 兼容模式（模式 A）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stub = start_stub_server()
        cls.client = OpenAICompatClient(cls.stub.base_url, "stub-key", STUB_MODEL)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stub.stop()

    def test_list_models(self) -> None:
        self.assertIn(STUB_MODEL, self.client.list_models())

    def test_chat_returns_content(self) -> None:
        content = self.client.chat([{"role": "user", "content": "生成一个脚本"}])
        data = json.loads(content)
        self.assertIn("shots", data)
        self.assertEqual(len(data["shots"]), 3)

    def test_test_connection_ok(self) -> None:
        result = self.client.test_connection()
        self.assertTrue(result["ok"])
        self.assertEqual(result["models"], [STUB_MODEL])
        self.assertIsInstance(result["latency_ms"], int)
        self.assertIn("连接成功", result["message"])

    def test_base_url_trailing_slash_stripped(self) -> None:
        client = OpenAICompatClient(self.stub.base_url + "/", "k", STUB_MODEL)
        self.assertFalse(client.base_url.endswith("/"))

    def test_chat_does_not_send_think_param(self) -> None:
        """OpenAI 兼容模式**不得**发送厂商私有 ``think`` 参数（标准端点会报错）。"""
        from tests.stub_llm_server import _Handler

        _Handler.bodies = []
        self.client.chat([{"role": "user", "content": "hi"}])
        self.assertTrue(_Handler.bodies)
        self.assertNotIn("think", _Handler.bodies[-1])

    def test_chat_applies_default_max_tokens(self) -> None:
        """未显式给 max_tokens 时应注入默认值，避免思考内容耗尽额度。"""
        from tests.stub_llm_server import _Handler

        _Handler.bodies = []
        self.client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(_Handler.bodies[-1].get("max_tokens"), DEFAULT_MAX_TOKENS)


class TestOllamaClient(unittest.TestCase):
    """Ollama 模式（模式 B）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stub = start_stub_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stub.stop()

    def test_list_models_via_api_tags(self) -> None:
        client = OllamaClient(self.stub.host, "qwen2.5:14b")
        models = client.list_models()
        self.assertIn(STUB_MODEL, models)
        self.assertIn("qwen2.5:14b", models)

    def test_chat_via_v1_endpoint(self) -> None:
        client = OllamaClient(self.stub.host, "qwen2.5:14b")
        content = client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("shots", json.loads(content))

    def test_host_normalizes_v1_suffix(self) -> None:
        client = OllamaClient("http://127.0.0.1:11434/v1", "m")
        self.assertEqual(client.host, "http://127.0.0.1:11434")
        self.assertTrue(client.base_url.endswith("/v1"))

    def test_direct_chat_url_is_not_models_endpoint(self) -> None:
        """验证 Ollama 对话确实走 /v1 而不是 /api/tags。"""
        from tests.stub_llm_server import _Handler

        _Handler.calls = []
        client = OllamaClient(self.stub.host, "m")
        client.chat([{"role": "user", "content": "x"}])
        self.assertTrue(any(call.startswith("POST /v1/chat/completions") for call in _Handler.calls))

    def test_chat_sends_think_false(self) -> None:
        """Ollama 模式必须在请求体带 ``think: false``（关闭思考，避免正文为空）。"""
        from tests.stub_llm_server import _Handler

        _Handler.bodies = []
        client = OllamaClient(self.stub.host, "qwen3:30b-a3b")
        client.chat([{"role": "user", "content": "hi"}])
        self.assertTrue(_Handler.bodies)
        body = _Handler.bodies[-1]
        self.assertIn("think", body)
        self.assertIs(body["think"], False)

    def test_chat_applies_default_max_tokens(self) -> None:
        """Ollama 模式未显式给 max_tokens 时应注入默认值。"""
        from tests.stub_llm_server import _Handler

        _Handler.bodies = []
        client = OllamaClient(self.stub.host, "m")
        client.chat([{"role": "user", "content": "x"}])
        self.assertEqual(_Handler.bodies[-1].get("max_tokens"), DEFAULT_MAX_TOKENS)

    def test_chat_respects_explicit_max_tokens(self) -> None:
        """显式传入的 max_tokens 应被尊重、不被默认值覆盖。"""
        from tests.stub_llm_server import _Handler

        _Handler.bodies = []
        client = OllamaClient(self.stub.host, "m")
        client.chat([{"role": "user", "content": "x"}], max_tokens=123)
        self.assertEqual(_Handler.bodies[-1].get("max_tokens"), 123)


class TestLLMErrorBranches(unittest.TestCase):
    """错误分支：Key 错误 / 不可达 / 空内容。"""

    def test_unauthorized(self) -> None:
        httpd = _make_server(_UnauthorizedHandler)
        port = httpd.server_address[1]
        try:
            client = OpenAICompatClient(f"http://127.0.0.1:{port}/v1", "bad", "m")
            result = client.test_connection()
            self.assertFalse(result["ok"])
            self.assertIn(ErrorCode.LLM_UNAUTHORIZED, result["message"])
            self.assertIn("API Key", result["message"])
            with self.assertRaises(LLMError) as ctx:
                client.chat([{"role": "user", "content": "x"}])
            self.assertEqual(ctx.exception.code, ErrorCode.LLM_UNAUTHORIZED)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_unreachable(self) -> None:
        # 指向一个几乎不可能有服务的端口
        client = OpenAICompatClient("http://127.0.0.1:9/v1", "k", "m")
        result = client.test_connection()
        self.assertFalse(result["ok"])
        self.assertIn(ErrorCode.LLM_UNREACHABLE, result["message"])
        with self.assertRaises(LLMError) as ctx:
            client.chat([{"role": "user", "content": "x"}], timeout=3.0)
        self.assertEqual(ctx.exception.code, ErrorCode.LLM_UNREACHABLE)

    def test_ollama_unreachable_message_is_chinese(self) -> None:
        client = OllamaClient("http://127.0.0.1:9", "m")
        with self.assertRaises(LLMError) as ctx:
            client.list_models()
        self.assertEqual(ctx.exception.code, ErrorCode.LLM_UNREACHABLE)
        self.assertIn("Ollama", ctx.exception.message)

    def test_empty_content(self) -> None:
        httpd = _make_server(_EmptyContentHandler)
        port = httpd.server_address[1]
        try:
            client = OpenAICompatClient(f"http://127.0.0.1:{port}/v1", "k", "m")
            with self.assertRaises(LLMError) as ctx:
                client.chat([{"role": "user", "content": "x"}])
            self.assertEqual(ctx.exception.code, ErrorCode.LLM_REQUEST_FAILED)
            self.assertIn("空内容", ctx.exception.message)
            # 空内容不应导致 test_connection 崩溃（它走 /v1/models）
            self.assertTrue(client.test_connection()["ok"])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_thinking_only_raises_content_empty(self) -> None:
        """content 为空但存在 reasoning → 抛出「思考模式未产出正文」明确错误。"""
        httpd = _make_server(_ThinkingOnlyHandler)
        port = httpd.server_address[1]
        try:
            client = OpenAICompatClient(f"http://127.0.0.1:{port}/v1", "k", "m")
            with self.assertRaises(LLMError) as ctx:
                client.chat([{"role": "user", "content": "x"}])
            self.assertEqual(ctx.exception.code, ErrorCode.LLM_CONTENT_EMPTY)
            self.assertIn("思考模式", ctx.exception.message)
            self.assertIn("max_tokens", ctx.exception.message)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_ollama_thinking_only_raises_content_empty(self) -> None:
        """Ollama 模式下同一行为（关闭 think 后仍无正文）也须给出明确错误。"""
        httpd = _make_server(_ThinkingOnlyHandler)
        port = httpd.server_address[1]
        try:
            client = OllamaClient(f"http://127.0.0.1:{port}", "qwen3:30b-a3b")
            with self.assertRaises(LLMError) as ctx:
                client.chat([{"role": "user", "content": "x"}])
            self.assertEqual(ctx.exception.code, ErrorCode.LLM_CONTENT_EMPTY)
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestBuildClientFactory(unittest.TestCase):
    """工厂路由。"""

    def test_ollama_mode(self) -> None:
        cfg = Config(llm_mode=LLMMode.OLLAMA, llm_base_url="http://localhost:11434", llm_model="m")
        self.assertIsInstance(build_llm_client(cfg), OllamaClient)

    def test_openai_mode(self) -> None:
        cfg = Config(llm_mode=LLMMode.OPENAI, llm_base_url="https://x/v1", llm_api_key="k", llm_model="m")
        client = build_llm_client(cfg)
        self.assertIsInstance(client, OpenAICompatClient)
        self.assertNotIsInstance(client, OllamaClient)

    def test_both_implement_abstract_interface(self) -> None:
        self.assertTrue(issubclass(OpenAICompatClient, LLMClient))
        self.assertTrue(issubclass(OllamaClient, LLMClient))


if __name__ == "__main__":
    unittest.main(verbosity=2)
