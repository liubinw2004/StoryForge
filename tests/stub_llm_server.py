"""OpenAI 兼容桩服务（零第三方依赖，仅用标准库 ``http.server``）。

用途：
- 单元测试 ``LLMClient``（OpenAI / Ollama 两种模式的构造与错误分支）；
- 端到端冒烟：给 ``ScriptAgent`` / ``CriticAgent`` 返回确定性 JSON，避免真实
  Ollama CPU 推理（100–160s/次）拖垮测试。

端点：
- ``POST /v1/chat/completions``：按请求内容判别「脚本 / 脚本评分 / 视频评分」并返回固定 JSON；
- ``GET  /v1/models``：返回模型列表（OpenAI 模式 ``list_models``）；
- ``GET  /api/tags``：返回模型列表（Ollama 模式 ``list_models``）；
- ``GET  /health``：返回图像服务探活负载（``ready``，供 E2E 零外网验证）；
- ``POST /v1/images/generations``：返回一张 1×1 PNG 的 ``b64_json``（供 E2E 验证本地图像适配器）。

可作为模块（``start_stub_server``）在进程内启动，也可直接 ``python stub_llm_server.py 18080`` 独立运行。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STUB_MODEL = "stub-model"

# 1×1 透明 PNG（base64）：图像适配器只需「非空 b64_json → 解码落盘」，内容无视觉要求。
_STUB_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# ScriptAgent 需要的分镜脚本（固定 3 个镜头，duration=2，方便断言 Σ=6s）
SCRIPT_JSON = {
    "title": "咖啡馆的约定",
    "style": "realistic",
    "characters": [
        {"name": "小明", "desc": "20岁男生，短发，白色卫衣"},
        {"name": "小红", "desc": "20岁女生，长发，浅蓝色衬衫"},
    ],
    "scene": "午后阳光下的咖啡馆，靠窗座位",
    "positions": ["1. 小明坐在左侧", "2. 小红站在右侧"],
    "shots": [
        {
            "shot_id": "shot_001",
            "dialogue": "小红：我们一起来做点事吧",
            "action": "小红推门而入，环视咖啡馆",
            "camera": "中景，缓慢推进",
            "duration": 2,
            "prompt": "A girl pushing the door of a sunny cafe",
        },
        {
            "shot_id": "shot_002",
            "dialogue": "小明：我一直在等你这句话",
            "action": "小明抬头看向小红",
            "camera": "近景，固定",
            "duration": 2,
            "prompt": "A boy looking up, close up",
        },
        {
            "shot_id": "shot_003",
            "dialogue": "两人：那就一起加油",
            "action": "两人相视一笑，握拳",
            "camera": "全景，缓慢拉远",
            "duration": 2,
            "prompt": "Two friends smiling, wide shot",
        },
    ],
}

# StoryAgent 需要的完整剧情（固定 beats 4 条，synopsis 非空，便于断言 ≥3）
STORY_JSON = {
    "title": "咖啡馆的约定",
    "logline": "两个大学同学在咖啡馆重逢，决定一起创业。",
    "synopsis": (
        "午后阳光正好，小红推开咖啡馆的门，环顾四周，忽然看到一个熟悉的身影——"
        "那是大学同学小明。两人愣了一下，随后笑着打招呼，坐到了靠窗的座位。\n\n"
        "聊着聊着，小红提起自己一直想开一家小店，问小明要不要一起创业。"
        "小明沉默了很久，终于坦白说自己其实也有同样的想法，只是一直没敢说出口。"
        "窗外阳光正好，两人举起咖啡杯轻轻碰了一下，约定一起加油。"
    ),
    "setting": "现代都市，午后阳光下的咖啡馆，靠窗座位",
    "characters": [
        {"name": "小明", "desc": "20岁男生，短发，白色卫衣"},
        {"name": "小红", "desc": "20岁女生，长发，浅蓝色衬衫"},
    ],
    "beats": [
        {"index": 1, "summary": "小红走进咖啡馆，与小明意外重逢", "purpose": "铺垫", "location": "咖啡馆门口"},
        {"index": 2, "summary": "两人坐下来叙旧，聊起各自近况", "purpose": "推进", "location": "咖啡馆靠窗座位"},
        {"index": 3, "summary": "小红提出一起创业，小明陷入犹豫", "purpose": "冲突", "location": "咖啡馆靠窗座位"},
        {"index": 4, "summary": "小明坦白也有同样想法，两人达成约定", "purpose": "转折与收尾", "location": "咖啡馆"},
    ],
    "genre": "都市情感",
    "theme": "勇敢追梦与重逢的友情",
    "source_dialogue": "",
}

SCRIPT_SCORE_JSON = {
    "coherence": 8.5,
    "shootability": 8.0,
    "character_consistency": 8.4,
    "plot_completeness": 7.9,
    "comments": {
        "coherence": "情节推进自然",
        "shootability": "场景可实现",
        "character_consistency": "角色外观稳定",
        "plot_completeness": "首尾呼应",
    },
    "comment": "整体是一部结构完整的小短剧。",
}

VIDEO_SCORE_JSON = {
    "visual_consistency": 7.5,
    "dialogue_match": 7.8,
    "style_fidelity": 7.4,
    "comments": {
        "visual_consistency": "镜头间画面连贯",
        "dialogue_match": "画面与对白匹配",
        "style_fidelity": "风格统一",
    },
    "comment": "视频保真度良好，可继续优化转场。",
}


def _classify(messages: list[dict]) -> dict:
    """根据消息内容判别该返回哪种固定 JSON。"""
    text = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    if "visual_consistency" in text:
        return VIDEO_SCORE_JSON
    if "coherence" in text:
        return SCRIPT_SCORE_JSON
    # 剧情补全：story 的 system/user 提示词含 _STORY_SCHEMA（synopsis / beats）
    if "synopsis" in text or "beats" in text:
        return STORY_JSON
    return SCRIPT_JSON


class _Handler(BaseHTTPRequestHandler):
    """桩请求处理器；返回内容固定，行为可预测。"""

    protocol_version = "HTTP/1.1"

    # 记录收到的请求数，便于断言（如重试次数）
    calls: list[str] = []

    # 记录最近收到的 POST 请求体，便于断言请求参数（如 think / max_tokens）
    bodies: list[dict] = []

    def log_message(self, *args) -> None:  # noqa: D102 - 静默日志
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        _Handler.calls.append("GET " + self.path)
        if self.path.startswith("/v1/models"):
            self._send_json({"object": "list", "data": [{"id": STUB_MODEL, "object": "model"}]})
        elif self.path.startswith("/api/tags"):
            self._send_json({"models": [{"name": STUB_MODEL}, {"name": "qwen2.5:14b"}]})
        elif self.path.startswith("/health"):
            self._send_json(
                {"ok": True, "ready": True, "busy": False, "gpu": "mock-gpu", "ip_adapter": True}
            )
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        _Handler.calls.append("POST " + self.path)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}
        _Handler.bodies.append(payload if isinstance(payload, dict) else {})

        if self.path.startswith("/v1/images/generations"):
            # 本地图像服务契约：resp["data"][0]["b64_json"]；E2E 下返回确定性的 1×1 PNG。
            self._send_json(
                {
                    "data": [
                        {
                            "b64_json": _STUB_PNG_B64,
                            "size": str(payload.get("size") or "1280x720"),
                            "gen_size": "1024x1024",
                            "mode": "text-to-image",
                            "elapsed": 0.01,
                        }
                    ]
                }
            )
            return

        messages = payload.get("messages") or []
        content = json.dumps(_classify(messages), ensure_ascii=False)
        self._send_json(
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "model": STUB_MODEL,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


class StubServer:
    """可启停的桩服务封装。"""

    def __init__(self, port: int = 0) -> None:
        """在 127.0.0.1 上创建桩服务（port=0 表示自动分配端口）。"""
        _Handler.calls = []
        _Handler.bodies = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """OpenAI 兼容 base_url（含 ``/v1``）。"""
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def host(self) -> str:
        """主机地址（不含 ``/v1``，Ollama 模式用）。"""
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "StubServer":
        """在后台线程启动服务。"""
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """停止服务。"""
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=3)


def start_stub_server(port: int = 0) -> StubServer:
    """便捷函数：创建并启动桩服务。"""
    return StubServer(port).start()


if __name__ == "__main__":  # pragma: no cover - 独立运行入口
    import sys

    listen_port = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
    server = StubServer(listen_port)
    print(f"stub LLM server listening on http://127.0.0.1:{server.port}", flush=True)
    try:
        server._httpd.serve_forever()
    except KeyboardInterrupt:
        pass
