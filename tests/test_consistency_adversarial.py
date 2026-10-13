"""RunningHub 工作流 i2v —— **对抗性回归测试**。

把本次架构减法中确有价值的「证伪」断言固化为长期回归：

1. **提示词裁剪兜底**（prompt trim）：``build_video_prompt`` 在极小预算下 SHOT 主体
   永不截断、先丢尾部角色卡、再截 SCENE 卡；
2. **工作流 i2v 契约对抗**：``nodeInfoList`` 字段名必须是 ``fieldValue``（**不是**
   ``value``）、image/prompt 传字符串、duration/seed 传整数、首帧填上传返回的
   ``data.fileName``（**不是** URL）；
3. **删除彻底性（AC16）**：``ImageGenerator`` / ``EngineSwitcher`` / ``image_*`` 适配器 /
   ``IMAGE_ADAPTER_REGISTRY`` / ``ImageProvider`` / ``generate_image`` /
   ``supports_first_frame`` / ``build_anchor_prompt`` / ``DirectorContext`` 在 ``app/``
   源码（``*.py``）零残留；
4. **未配置即报错（AC15）**：``video_api_key`` 为空时 ``submit_workflow`` 明确抛
   ``VID_NOT_CONFIGURED``，绝不静默降级。

运行（**禁代理**）::

    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        .venv/Scripts/python.exe -m unittest tests.test_consistency_adversarial -v
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models.enums import VideoProvider
from app.models.schemas import Config
from app.services.adapters import runninghub_adapter
from app.services.adapters.base import VideoError
from app.services.adapters.runninghub_adapter import RunningHubAdapter
from app.services.prompt_templates import build_video_prompt
from app.utils.responses import ErrorCode


# --------------------------------------------------------------------------- #
# 1) 提示词裁剪兜底
# --------------------------------------------------------------------------- #
class TestPromptTrimAdversarial(unittest.TestCase):
    """SHOT 永不截断；超预算先丢尾部角色卡、再截 SCENE。"""

    def test_shot_survives_tiny_budget(self) -> None:
        shot = "SHOT_" + "z" * 500
        out = build_video_prompt(
            shot, "realistic", ["A" * 200, "B" * 200, "C" * 200], "S" * 600, budget=50
        )
        self.assertIn(shot, out, "SHOT 主体在极小预算下仍不得截断")

    def test_trailing_cards_dropped_before_scene(self) -> None:
        out = build_video_prompt(
            "body", "realistic", ["A" * 200, "B" * 200, "C" * 200], "S" * 600, budget=400
        )
        self.assertIn("A" * 200, out)
        self.assertNotIn("B" * 200, out)
        self.assertNotIn("C" * 200, out)


# --------------------------------------------------------------------------- #
# 2) 工作流 i2v 契约对抗
# --------------------------------------------------------------------------- #
def _adapter(**overrides) -> RunningHubAdapter:
    kwargs = {
        "video_provider": VideoProvider.RUNNINGHUB,
        "video_api_key": "dummy-key",
        "video_base_url": "https://www.runninghub.cn",
    }
    kwargs.update(overrides)
    return RunningHubAdapter(Config(**kwargs))


class _FakeResponse:
    """最小 HTTP 响应替身（供 httpx.post 打桩）。"""

    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class TestWorkflowContractAdversarial(unittest.TestCase):
    """锁死工作流 i2v 的 nodeInfoList 契约与首帧 fileName 语义。"""

    def test_build_node_info_list_uses_fieldvalue_not_value(self) -> None:
        adapter = _adapter()
        nodes = adapter.build_node_info_list("a prompt", 4, 42, "openapi/frame.png")
        self.assertEqual(len(nodes), 4)
        for node in nodes:
            self.assertIn("fieldValue", node, "字段名必须是 fieldValue")
            self.assertNotIn("value", node, "不得使用旧字段名 value")
            self.assertIn("nodeId", node)
            self.assertIn("fieldName", node)
        # image/prompt 传字符串；duration/seed 传整数
        self.assertIsInstance(nodes[0]["fieldValue"], str)
        self.assertIsInstance(nodes[1]["fieldValue"], str)
        self.assertIsInstance(nodes[2]["fieldValue"], int)
        self.assertIsInstance(nodes[3]["fieldValue"], int)
        self.assertEqual(nodes[0]["fieldValue"], "openapi/frame.png")
        self.assertEqual(nodes[1]["fieldValue"], "a prompt")
        self.assertEqual(nodes[2]["fieldValue"], 4)
        self.assertEqual(nodes[3]["fieldValue"], 42)

    def test_upload_first_frame_returns_file_name_not_url(self) -> None:
        adapter = _adapter()
        payload = {
            "code": 0,
            "data": {
                "fileName": "openapi/frame.png",
                "download_url": "https://cdn.example.invalid/frame.png",
            },
        }
        with tempfile.TemporaryDirectory(prefix="sf_adv_upload_") as tmp:
            img = Path(tmp) / "frame.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"X" * 32)
            with mock.patch.object(
                runninghub_adapter.httpx, "post", return_value=_FakeResponse(payload)
            ) as post:
                file_name = adapter.upload_first_frame(str(img))
            self.assertEqual(file_name, "openapi/frame.png", "必须取 data.fileName（非 URL）")
            self.assertEqual(post.call_count, 1)

    def test_upload_first_frame_missing_file_name_raises(self) -> None:
        adapter = _adapter()
        with tempfile.TemporaryDirectory(prefix="sf_adv_upload2_") as tmp:
            img = Path(tmp) / "frame.png"
            img.write_bytes(b"X" * 8)
            with mock.patch.object(
                runninghub_adapter.httpx,
                "post",
                return_value=_FakeResponse({"code": 0, "data": {"download_url": "u"}}),
            ):
                with self.assertRaises(VideoError) as ctx:
                    adapter.upload_first_frame(str(img))
            self.assertEqual(ctx.exception.code, ErrorCode.VID_API_ERROR)
            self.assertIn("fileName", str(ctx.exception))

    def test_submit_workflow_body_shape(self) -> None:
        adapter = _adapter(rh_workflow_id="wf-1", rh_instance_type="plus", rh_retain_seconds=30)
        nodes = adapter.build_node_info_list("p", 4, 7, "openapi/frame.png")
        captured: dict = {}

        def _post(url, json=None, **kwargs):  # noqa: ANN001
            captured["url"] = url
            captured["body"] = json
            return _FakeResponse({"taskId": "task-abc", "status": "RUNNING",
                                  "errorCode": "", "errorMessage": ""})

        with mock.patch.object(runninghub_adapter.httpx, "post", side_effect=_post):
            task_id = adapter.submit_workflow(nodes, "plus", 30)
        self.assertEqual(task_id, "task-abc")
        self.assertIn("/openapi/v2/run/workflow/wf-1", captured["url"])
        body = captured["body"]
        self.assertEqual(body["nodeInfoList"], nodes)
        self.assertEqual(body["instanceType"], "plus")
        self.assertIs(body["usePersonalQueue"], False)
        self.assertEqual(body["retainSeconds"], 30)

    def test_submit_workflow_missing_key_raises_not_configured(self) -> None:
        adapter = _adapter(video_api_key="")
        with self.assertRaises(VideoError) as ctx:
            adapter.submit_workflow([], "default", 180)
        self.assertEqual(ctx.exception.code, ErrorCode.VID_NOT_CONFIGURED)
        self.assertIn("API Key", str(ctx.exception))

    def test_submit_workflow_missing_workflow_id_raises(self) -> None:
        adapter = _adapter(rh_workflow_id="")
        with self.assertRaises(VideoError) as ctx:
            adapter.submit_workflow([], "default", 180)
        self.assertEqual(ctx.exception.code, ErrorCode.VID_NOT_CONFIGURED)
        self.assertIn("工作流 ID", str(ctx.exception))


# --------------------------------------------------------------------------- #
# 3) 删除彻底性（AC16）
# --------------------------------------------------------------------------- #
class TestDeletionThoroughness(unittest.TestCase):
    """自建图像生成 / 引擎切换 / 图像路由轴在 app/ 源码零残留。"""

    RESIDUE_TOKENS = (
        "ImageGenerator",
        "EngineSwitcher",
        "IMAGE_ADAPTER_REGISTRY",
        "ImageProvider",
        "generate_image",
        "supports_first_frame",
        "build_anchor_prompt",
        "DirectorContext",
        "chain_enabled",
        "retry_shot",
    )

    REMOVED_FILES = (
        "image_generator.py",
        "engine_switch.py",
        "image_base.py",
        "image_mock_adapter.py",
        "image_local_adapter.py",
        "image_runninghub_adapter.py",
    )

    def test_no_residue_tokens_in_app_py(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        app_dir = repo / "app"
        hits: list[str] = []
        for py in app_dir.rglob("*.py"):
            text = py.read_text(encoding="utf-8", errors="ignore")
            for token in self.RESIDUE_TOKENS:
                if token in text:
                    hits.append(f"{py}:{token}")
        self.assertEqual(hits, [], f"app/ 源码残留已删除组件：{hits}")

    def test_removed_adapter_files_absent(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        adapters_dir = repo / "app" / "services" / "adapters"
        files = {p.name for p in adapters_dir.glob("*.py")}
        for removed in self.REMOVED_FILES:
            self.assertNotIn(removed, files, f"残留文件 {removed}")

    def test_deploy_image_server_removed(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        self.assertFalse((repo / "deploy" / "image_server").exists(), "deploy/image_server 应已删除")

    def test_adapters_registry_only_video(self) -> None:
        from app.services.adapters import ADAPTER_REGISTRY
        self.assertEqual(set(ADAPTER_REGISTRY), {VideoProvider.MOCK, VideoProvider.RUNNINGHUB})

    def test_enum_only_video(self) -> None:
        self.assertEqual({p.value for p in VideoProvider}, {"mock", "runninghub"})


# --------------------------------------------------------------------------- #
# 4) 零外网（AC18）
# --------------------------------------------------------------------------- #
class TestZeroExternalNetwork(unittest.TestCase):
    """RunningHub 适配器源码只经 httpx 出口；单测全程打桩，绝不真连外网。"""

    def test_source_uses_httpx_for_all_network(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        src = (repo / "app" / "services" / "adapters" / "runninghub_adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("httpx.post(", src)
        self.assertIn("httpx.stream(", src)
        self.assertNotIn("requests.", src)
        self.assertNotIn("urllib.request", src)

    def test_workflow_path_template_is_relative(self) -> None:
        self.assertEqual(
            runninghub_adapter.WORKFLOW_RUN_PATH_TEMPLATE,
            "/openapi/v2/run/workflow/{workflow_id}",
        )
        self.assertEqual(runninghub_adapter.UPLOAD_PATH, "/openapi/v2/media/upload/binary")
        self.assertEqual(runninghub_adapter.QUERY_PATH, "/openapi/v2/query")

    def test_frontend_has_no_external_links(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        html = (repo / "app" / "static" / "index.html").read_text(encoding="utf-8")
        external = [
            m for m in re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', html)
            if m.startswith("http://") or m.startswith("https://") or m.startswith("//")
        ]
        self.assertEqual(external, [], f"index.html 存在外链：{external}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
