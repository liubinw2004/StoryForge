"""增量补充测试：增量 API（``GET /story``、``POST /export``）与路径穿越防护回归。

通过 ``fastapi.testclient.TestClient`` 直接打到路由层（不启动真实服务、不联网）。

运行：``.venv/Scripts/python.exe -m unittest tests.test_api_increment -v``
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.deps import get_task_manager, reset_singletons
from app.main import app
from app.models.enums import ShotState
from app.models.schemas import Config, ScriptDocument, Shot, ShotResult, StoryDocument
from app.utils.responses import ErrorCode


class _ApiCase(unittest.TestCase):
    """共享：临时任务目录 + 全局 TaskManager 注入。"""

    def setUp(self) -> None:
        reset_singletons()
        self.client = TestClient(app)
        self.client.__enter__()
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_api_"))
        self.mgr = get_task_manager()
        self.tid = self.mgr.create_task(self.tmp, Config())

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        reset_singletons()


class TestGetStoryEndpoint(_ApiCase):
    """``GET /api/pipeline/{task_id}/story``。"""

    def test_story_not_generated_returns_task_not_found(self) -> None:
        res = self.client.get(f"/api/pipeline/{self.tid}/story")
        body = res.json()
        self.assertIs(body.get("ok"), False)
        self.assertIn(ErrorCode.TASK_NOT_FOUND, body.get("error") or "")

    def test_unknown_task_returns_task_not_found(self) -> None:
        res = self.client.get("/api/pipeline/deadbeefcafe/story")
        body = res.json()
        self.assertIs(body.get("ok"), False)
        self.assertIn(ErrorCode.TASK_NOT_FOUND, body.get("error") or "")

    def test_story_generated_returns_document(self) -> None:
        self.mgr._records[self.tid].story = StoryDocument(
            title="剧情标题", synopsis="正文", beats=[]
        )
        res = self.client.get(f"/api/pipeline/{self.tid}/story")
        body = res.json()
        self.assertIs(body.get("ok"), True)
        self.assertEqual(body["data"]["title"], "剧情标题")


class TestExportEndpointErrors(_ApiCase):
    """``POST /api/pipeline/{task_id}/export`` 异常路径（统一响应，不抛 500）。"""

    def test_unknown_task(self) -> None:
        res = self.client.post("/api/pipeline/deadbeefcafe/export", json={"clips": []})
        body = res.json()
        self.assertIs(body.get("ok"), False)
        self.assertIn(ErrorCode.TASK_NOT_FOUND, body.get("error") or "")

    def test_script_missing(self) -> None:
        res = self.client.post(f"/api/pipeline/{self.tid}/export", json={"clips": []})
        body = res.json()
        self.assertIs(body.get("ok"), False)
        self.assertIn(ErrorCode.TASK_FAILED, body.get("error") or "")

    def test_no_successful_shots(self) -> None:
        self.mgr._records[self.tid].script = ScriptDocument(
            title="t", shots=[Shot(shot_id="shot_001")]
        )
        self.mgr.update_shot(
            self.tid, ShotResult(shot_id="shot_001", state=ShotState.FAILED, duration=0.0)
        )
        res = self.client.post(
            f"/api/pipeline/{self.tid}/export",
            json={"clips": [{"shot_id": "shot_001", "enabled": True}]},
        )
        body = res.json()
        self.assertIs(body.get("ok"), False)
        self.assertIn(ErrorCode.SYS_BAD_REQUEST, body.get("error") or "")

    def test_empty_clips_no_available(self) -> None:
        self.mgr._records[self.tid].script = ScriptDocument(
            title="t", shots=[Shot(shot_id="shot_001")]
        )
        self.mgr.update_shot(
            self.tid, ShotResult(shot_id="shot_001", state=ShotState.FAILED, duration=0.0)
        )
        res = self.client.post(f"/api/pipeline/{self.tid}/export", json={"clips": []})
        body = res.json()
        self.assertIs(body.get("ok"), False)
        self.assertIn(ErrorCode.SYS_BAD_REQUEST, body.get("error") or "")


class TestPathTraversalGuard(_ApiCase):
    """路径穿越防护回归（安全项）：``download-artifact`` 必须拒绝越权读取。"""

    def test_download_artifact_dotdot_rejected(self) -> None:
        for name in ("../config/config.json", "../../etc/passwd", "..\\..\\config\\config.json"):
            res = self.client.get(
                f"/api/pipeline/{self.tid}/download-artifact", params={"name": name}
            )
            self.assertEqual(res.status_code, 400, f"{name} 未被拒绝：HTTP {res.status_code}")
            # 不得泄漏配置内容（llm_api_key 等）
            self.assertNotIn("llm_api_key", res.text)
            self.assertNotIn("root:0:0", res.text)

    def test_download_artifact_normal_file_allowed(self) -> None:
        """正向对照：任务目录内的正常文件仍可下载（防护未过度拦截）。"""
        target = self.tmp / "story.json"
        target.write_text('{"title": "ok"}', encoding="utf-8")
        res = self.client.get(
            f"/api/pipeline/{self.tid}/download-artifact", params={"name": "story.json"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("attachment", res.headers.get("content-disposition", ""))

    def test_video_endpoint_dotdot_rejected(self) -> None:
        res = self.client.get(
            f"/api/pipeline/{self.tid}/video/..%2f..%2f..%2fconfig%2fconfig.json"
        )
        self.assertEqual(res.status_code, 400, f"HTTP {res.status_code}")
        self.assertNotIn("llm_api_key", res.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
