"""增量补充测试：前端静态一致性（零构建、零 CDN 约束）。

校验：
1. ``index.html`` 的 ``data-stage`` 卡片集合与 ``app.js`` 的 ``STAGE_KEYS`` 同值同序；
2. ``app.js`` 中所有 ``$("xxx")`` 引用的元素 id 均存在于 ``index.html``；
3. ``index.html`` 零外链（无 ``http(s)://`` / ``//`` 的 ``src``/``href``）；
4. 串行首帧链（③.6 逐段确认区）与 RunningHub 工作流 i2v 配置 id 全部落盘。

运行：``.venv/Scripts/python.exe -m unittest tests.test_frontend_consistency -v``
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from app.utils.paths import Paths

_HTML = Paths.STATIC_DIR / "index.html"
_JS = Paths.STATIC_DIR / "app.js"

_ATTR_URL_RE = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']""")
_DATA_STAGE_RE = re.compile(r'data-stage\s*=\s*"([^"]+)"')
_STAGE_KEYS_RE = re.compile(r"var\s+STAGE_KEYS\s*=\s*\[([^\]]*)\]")
_ID_ATTR_RE = re.compile(r'\bid\s*=\s*"([^"]+)"')
_DOLLAR_ID_RE = re.compile(r"""\$\(\s*["']([^"']+)["']\s*\)""")


class TestFrontendConsistency(unittest.TestCase):
    """前端静态一致性。"""

    # 增量（第三批）：app.js 会 $() 引用的确认区 / 时间轴新 id，必须在 index.html 落盘。
    REVIEW_AND_TIMELINE_IDS = {
        "section-review", "reviewStateChip", "reviewTitle", "reviewGenre",
        "reviewLogline", "reviewSetting", "reviewSynopsis", "reviewCharsView",
        "reviewBeatsView", "reviewShotList", "reviewHint", "reviewSkipBtn",
        "reviewConfirmBtn", "reviewToggle",
        "timelineRuler", "timelinePlayhead", "splitClipBtn",
    }

    # 增量（RunningHub 工作流 i2v）：③.6 逐段确认区新增 id，必须在 index.html 落盘。
    CHAIN_IDS = {
        "section-chain", "chainChip", "chainHint", "chainStartArea",
        "chainFileInput", "chainStartBtn", "chainSegments",
    }

    # 增量（RunningHub 工作流 i2v）：视频配置组新增的 rh_* 输入 id，必须在 index.html 落盘。
    RH_CONFIG_IDS = {
        "rhWorkflowId", "rhInstanceType", "rhRetainSeconds",
        "rhFirstFrameNodeId", "rhFirstFrameFieldName",
        "rhPromptNodeId", "rhPromptFieldName",
        "rhDurationNodeId", "rhDurationFieldName",
        "rhSeedNodeId", "rhSeedFieldName",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = _HTML.read_text(encoding="utf-8")
        cls.js = _JS.read_text(encoding="utf-8")

    def test_stage_cards_match_stage_keys_order(self) -> None:
        cards = _DATA_STAGE_RE.findall(self.html)
        match = _STAGE_KEYS_RE.search(self.js)
        self.assertIsNotNone(match, "未在 app.js 中找到 STAGE_KEYS 定义")
        keys = re.findall(r"""["']([^"']+)["']""", match.group(1))
        self.assertEqual(cards, keys, f"卡片={cards} 与 STAGE_KEYS={keys} 不一致")
        self.assertEqual(keys[0], "story", "story 必须排在最前")
        self.assertEqual(len(keys), 5)

    def test_all_dollar_ids_exist_in_html(self) -> None:
        html_ids = set(_ID_ATTR_RE.findall(self.html))
        referenced = sorted(set(_DOLLAR_ID_RE.findall(self.js)))
        self.assertTrue(referenced, "未解析到任何 $() 引用，正则可能失效")
        missing = [name for name in referenced if name not in html_ids]
        self.assertEqual(missing, [], f"app.js 引用了 index.html 中不存在的 id：{missing}")

    def test_index_html_has_no_external_links(self) -> None:
        external = [
            url
            for url in _ATTR_URL_RE.findall(self.html)
            if url.startswith("http://") or url.startswith("https://") or url.startswith("//")
        ]
        self.assertEqual(external, [], f"index.html 存在外链：{external}")

    def test_static_assets_are_relative(self) -> None:
        for url in _ATTR_URL_RE.findall(self.html):
            self.assertTrue(
                url.startswith("/static/") or url.startswith("/") or not url.startswith("http"),
                f"静态资源应为本地相对路径：{url}",
            )

    def test_review_and_timeline_ids_exist(self) -> None:
        """确认区 / 时间轴新增 id 必须全部存在于 index.html。"""
        html_ids = set(_ID_ATTR_RE.findall(self.html))
        missing = sorted(i for i in self.REVIEW_AND_TIMELINE_IDS if i not in html_ids)
        self.assertEqual(missing, [], f"index.html 缺少增量 id：{missing}")

    def test_chain_ids_exist(self) -> None:
        """逐段确认区（③.6）新增 id 必须全部存在于 index.html。"""
        html_ids = set(_ID_ATTR_RE.findall(self.html))
        missing = sorted(i for i in self.CHAIN_IDS if i not in html_ids)
        self.assertEqual(missing, [], f"index.html 缺少逐段确认区 id：{missing}")

    def test_rh_config_ids_exist(self) -> None:
        """RunningHub 工作流 i2v 配置组新增的 rh_* 输入 id 必须全部存在。"""
        html_ids = set(_ID_ATTR_RE.findall(self.html))
        missing = sorted(i for i in self.RH_CONFIG_IDS if i not in html_ids)
        self.assertEqual(missing, [], f"index.html 缺少 rh_* 配置 id：{missing}")

    def test_no_image_config_ids_remain(self) -> None:
        """自建图像生成配置组已整体移除：这些 id 不得再出现于 index.html。"""
        html_ids = set(_ID_ATTR_RE.findall(self.html))
        removed = {
            "imageEndpoint", "imageApiKey", "ipScale", "ipFace", "imageSeed",
            "shotFirstFrame", "autoEngineSwitch", "imageSwitchStartCmd",
            "imageSwitchStopCmd", "testImageBtn", "imageConfigFields",
        }
        self.assertEqual(removed & html_ids, set(), f"index.html 残留图像配置 id：{removed & html_ids}")

    def test_edit_list_id_preserved_as_timeline_track(self) -> None:
        """时间轴轨道保留原 ``#editList`` id（避免破坏既有引用）。"""
        html_ids = set(_ID_ATTR_RE.findall(self.html))
        self.assertIn("editList", html_ids)

    def test_confirmation_card_does_not_carry_data_stage(self) -> None:
        """确认区不得携带 ``data-stage``（护阶段卡片恒为 5、story 最前）。"""
        cards = _DATA_STAGE_RE.findall(self.html)
        self.assertEqual(len(cards), 5)
        self.assertEqual(cards[0], "story")


if __name__ == "__main__":
    unittest.main(verbosity=2)
