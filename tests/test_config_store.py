"""单元测试：``app.services.config_store.ConfigStore`` 脱敏 / 回填边界。

运行：``.venv/Scripts/python.exe -m unittest tests.test_config_store -v``
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.models.schemas import Config, mask_secret
from app.services.config_store import ConfigStore

REAL_KEY = "sk-abcdefghijklmnop-1234"  # 长度 > 8，可脱敏


class TestMaskSecret(unittest.TestCase):
    """脱敏函数边界。"""

    def test_empty(self) -> None:
        self.assertEqual(mask_secret(""), "")

    def test_short_is_starred(self) -> None:
        self.assertEqual(mask_secret("12345678"), "****")
        self.assertEqual(mask_secret("abc"), "****")

    def test_long_keeps_head_tail(self) -> None:
        masked = mask_secret(REAL_KEY)
        self.assertEqual(masked, f"{REAL_KEY[:3]}****{REAL_KEY[-4:]}")
        self.assertNotIn("ghijklmnop", masked)


class TestConfigStore(unittest.TestCase):
    """读写 / 脱敏 / 回填。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_test_cfg_"))
        self.path = self.tmp / "config.json"

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_created_when_missing(self) -> None:
        store = ConfigStore(self.path)
        self.assertTrue(self.path.exists())
        cfg = store.get_raw()
        self.assertEqual(cfg.llm_mode.value, "ollama")
        self.assertEqual(cfg.llm_base_url, "http://localhost:11434")
        self.assertEqual(cfg.llm_model, "qwen2.5:14b")
        self.assertEqual(cfg.video_provider.value, "mock")
        self.assertEqual(cfg.shot_count, 4)
        self.assertEqual(cfg.shot_duration, 4)

    def test_public_masks_keys(self) -> None:
        store = ConfigStore(self.path)
        store.save(Config(llm_api_key=REAL_KEY, video_api_key=REAL_KEY))
        public = store.get_public()
        self.assertEqual(public.llm_api_key, mask_secret(REAL_KEY))
        self.assertEqual(public.video_api_key, mask_secret(REAL_KEY))
        # 磁盘仍为明文
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["llm_api_key"], REAL_KEY)

    def test_save_masked_value_preserves_raw(self) -> None:
        """MASK_ROUNDTRIP：回填脱敏值时磁盘保留原值。"""
        store = ConfigStore(self.path)
        store.save(Config(llm_api_key=REAL_KEY))
        # 模拟前端把脱敏值原样回传
        store.save(Config(llm_api_key=mask_secret(REAL_KEY)))
        self.assertEqual(store.get_raw().llm_api_key, REAL_KEY)
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["llm_api_key"], REAL_KEY)

    def test_save_real_value_replaces(self) -> None:
        store = ConfigStore(self.path)
        store.save(Config(llm_api_key=REAL_KEY))
        new_key = "sk-newkey-0000111122223333"
        store.save(Config(llm_api_key=new_key))
        self.assertEqual(store.get_raw().llm_api_key, new_key)

    def test_save_empty_string_behaviour(self) -> None:
        """边界：回填空串时的实测行为（记录事实，不判为 bug）。

        空串既非脱敏值也非真实值，会被直接写入 → **清空** 原 key。
        对应用户「主动删除 Key」的语义合理；记录以避免误判。
        """
        store = ConfigStore(self.path)
        store.save(Config(llm_api_key=REAL_KEY))
        store.save(Config(llm_api_key=""))
        self.assertEqual(store.get_raw().llm_api_key, "")

    def test_corrupt_file_resets_to_default(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ this is not valid json", encoding="utf-8")
        store = ConfigStore(self.path)
        self.assertEqual(store.get_raw().llm_mode.value, "ollama")
        # 应被重写为合法 JSON
        json.loads(self.path.read_text(encoding="utf-8"))

    def test_removed_video_provider_falls_back_to_mock(self) -> None:
        """历史 config.json 残留已下线 provider（kling/seedance/minimax/generic）→ 回退 mock。

        关键：**只**替换 ``video_provider``，其它用户设置（如 LLM/画幅）必须原样保留，
        绝不能因未知枚举值而重置整份配置。
        """
        for legacy in ("kling", "seedance", "minimax", "generic"):
            with self.subTest(legacy=legacy):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(
                        {
                            "llm_mode": "openai",
                            "llm_base_url": "https://api.deepseek.com/v1",
                            "llm_model": "deepseek-chat",
                            "shot_count": 6,
                            "video_provider": legacy,
                        }
                    ),
                    encoding="utf-8",
                )
                cfg = ConfigStore(self.path).get_raw()
                self.assertEqual(cfg.video_provider.value, "mock", f"{legacy} 应回退为 mock")
                # 其它字段保持不变
                self.assertEqual(cfg.llm_mode.value, "openai")
                self.assertEqual(cfg.llm_model, "deepseek-chat")
                self.assertEqual(cfg.shot_count, 6)

    def test_unknown_string_provider_falls_back_to_mock(self) -> None:
        """P2-1：**任何**非法字符串（不止已知下线名单，如拼写错误 ``runninbghub`` / ``foo``）
        都必须回退 ``mock``，且其它字段逐字段不变（绝不整份重置）。"""
        for bad in ("foo", "runninbghub", "RUNNINGHUB", "mock "):
            with self.subTest(bad=bad):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(
                        {
                            "llm_mode": "openai",
                            "llm_model": "deepseek-chat",
                            "shot_count": 6,
                            "shot_duration": 9,
                            "video_provider": bad,
                        }
                    ),
                    encoding="utf-8",
                )
                cfg = ConfigStore(self.path).get_raw()
                self.assertEqual(cfg.video_provider.value, "mock", f"{bad!r} 应回退为 mock")
                # 其它字段逐字段不变 + 反向断言（绝非默认值）
                self.assertEqual(cfg.llm_mode.value, "openai")
                self.assertEqual(cfg.llm_model, "deepseek-chat")
                self.assertEqual(cfg.shot_count, 6)
                self.assertEqual(cfg.shot_duration, 9)
                self.assertNotEqual(cfg.llm_model, "qwen2.5:14b")

    def test_non_string_provider_falls_back_to_mock(self) -> None:
        """P2-1：非字符串类型（如数字 ``123``）同样回退 ``mock``，不得因类型异常走整份重置。"""
        for bad in (123, 4.5, True, [], {"a": 1}):
            with self.subTest(bad=bad):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps({"video_provider": bad, "llm_model": "deepseek-chat"}),
                    encoding="utf-8",
                )
                cfg = ConfigStore(self.path).get_raw()
                self.assertEqual(cfg.video_provider.value, "mock", f"{bad!r} 应回退为 mock")
                self.assertEqual(cfg.llm_model, "deepseek-chat")

    def test_null_provider_falls_back_to_mock_not_reset(self) -> None:
        """P2-1：``video_provider: null`` 也按缺省处理为 ``mock``，**不得整份重置**。

        显式 null 若不处理，会因枚举校验失败而把用户的 LLM/画幅设置一并清空。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "video_provider": None,
                    "llm_mode": "openai",
                    "llm_model": "deepseek-chat",
                    "shot_count": 6,
                    "shot_duration": 9,
                }
            ),
            encoding="utf-8",
        )
        cfg = ConfigStore(self.path).get_raw()
        self.assertEqual(cfg.video_provider.value, "mock")
        # 反向断言：其它字段绝非默认值（即未被整份重置）
        self.assertEqual(cfg.llm_mode.value, "openai")
        self.assertEqual(cfg.llm_model, "deepseek-chat")
        self.assertEqual(cfg.shot_count, 6)
        self.assertEqual(cfg.shot_duration, 9)
        self.assertNotEqual(cfg.llm_model, "qwen2.5:14b")

    def test_valid_providers_are_not_migrated(self) -> None:
        """合法值 ``mock`` / ``runninghub`` 原样保留、不被回退。"""
        for keep in ("mock", "runninghub"):
            with self.subTest(keep=keep):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps({"video_provider": keep, "shot_count": 5}),
                    encoding="utf-8",
                )
                cfg = ConfigStore(self.path).get_raw()
                self.assertEqual(cfg.video_provider.value, keep)
                self.assertEqual(cfg.shot_count, 5)

    def test_persistence_roundtrip(self) -> None:
        store = ConfigStore(self.path)
        store.save(
            Config(
                llm_mode="openai",
                llm_base_url="https://api.deepseek.com/v1",
                llm_model="deepseek-chat",
                shot_count=6,
                shot_duration=5,
                skip_video_eval=True,
            )
        )
        reloaded = ConfigStore(self.path).get_raw()
        self.assertEqual(reloaded.llm_mode.value, "openai")
        self.assertEqual(reloaded.shot_count, 6)
        self.assertEqual(reloaded.shot_duration, 5)
        self.assertTrue(reloaded.skip_video_eval)


class TestRhConfigSanitization(unittest.TestCase):
    """RunningHub 工作流 i2v 的 ``rh_*`` 配置成本安全校验（AC13 / AC14）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf_test_rhcfg_"))
        self.path = self.tmp / "config.json"

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, payload: dict) -> Config:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return ConfigStore(self.path).get_raw()

    def test_defaults_applied_when_missing(self) -> None:
        cfg = self._load({"llm_model": "deepseek-chat"})
        self.assertEqual(cfg.rh_workflow_id, "2084067455902765057")
        self.assertEqual(cfg.rh_instance_type, "default")
        self.assertEqual(cfg.rh_retain_seconds, 180)
        self.assertEqual(cfg.rh_first_frame_node_id, "4")
        self.assertEqual(cfg.rh_first_frame_field_name, "image")
        self.assertEqual(cfg.rh_prompt_node_id, "7")
        self.assertEqual(cfg.rh_prompt_field_name, "prompt")
        self.assertEqual(cfg.rh_duration_node_id, "6")
        self.assertEqual(cfg.rh_duration_field_name, "duration_seconds")
        self.assertEqual(cfg.rh_seed_node_id, "9")
        self.assertEqual(cfg.rh_seed_field_name, "seed")

    def test_invalid_instance_type_falls_back_to_default(self) -> None:
        for bad in ("xxx", "", 123, None, [], {"a": 1}):
            with self.subTest(bad=bad):
                cfg = self._load({"rh_instance_type": bad, "llm_model": "m"})
                self.assertEqual(cfg.rh_instance_type, "default", f"{bad!r} 应回退 default")

    def test_valid_instance_types_kept(self) -> None:
        for keep in ("default", "plus", "ultra"):
            with self.subTest(keep=keep):
                cfg = self._load({"rh_instance_type": keep})
                self.assertEqual(cfg.rh_instance_type, keep)

    def test_retain_seconds_clamped_to_10_180(self) -> None:
        self.assertEqual(self._load({"rh_retain_seconds": 5}).rh_retain_seconds, 10)
        self.assertEqual(self._load({"rh_retain_seconds": 999}).rh_retain_seconds, 180)
        self.assertEqual(self._load({"rh_retain_seconds": 60}).rh_retain_seconds, 60)

    def test_retain_seconds_non_int_removed_to_default(self) -> None:
        for bad in ("180", 10.5, True, None, [], {"a": 1}):
            with self.subTest(bad=bad):
                cfg = self._load({"rh_retain_seconds": bad})
                self.assertEqual(cfg.rh_retain_seconds, 180, f"{bad!r} 应回落默认 180")

    def test_rh_string_fields_non_string_or_empty_removed(self) -> None:
        fields = (
            "rh_workflow_id",
            "rh_first_frame_node_id",
            "rh_first_frame_field_name",
            "rh_prompt_node_id",
            "rh_prompt_field_name",
            "rh_duration_node_id",
            "rh_duration_field_name",
            "rh_seed_node_id",
            "rh_seed_field_name",
        )
        for field in fields:
            for bad in ("", "   ", 123, None, [], {"a": 1}):
                with self.subTest(field=field, bad=bad):
                    cfg = self._load({field: bad})
                    # 非法值 → 删键 → 回落 Pydantic 字段默认值
                    default = getattr(Config(), field)
                    self.assertEqual(getattr(cfg, field), default, f"{field}={bad!r} 未回落默认")

    def test_rh_workflow_id_empty_keeps_default(self) -> None:
        cfg = self._load({"rh_workflow_id": ""})
        self.assertEqual(cfg.rh_workflow_id, "2084067455902765057")

    def test_valid_rh_values_untouched(self) -> None:
        cfg = self._load(
            {
                "rh_workflow_id": "wf-123",
                "rh_instance_type": "ultra",
                "rh_retain_seconds": 30,
                "rh_first_frame_node_id": "10",
                "rh_first_frame_field_name": "img",
            }
        )
        self.assertEqual(cfg.rh_workflow_id, "wf-123")
        self.assertEqual(cfg.rh_instance_type, "ultra")
        self.assertEqual(cfg.rh_retain_seconds, 30)
        self.assertEqual(cfg.rh_first_frame_node_id, "10")
        self.assertEqual(cfg.rh_first_frame_field_name, "img")

    def test_removed_image_fields_are_not_restored(self) -> None:
        """自建图像生成字段（image_* / chain_continuity / anchor_image / video_image_endpoint）
        已从 Config 删除：写入后不得回显。"""
        cfg = self._load(
            {
                "image_provider": "local",
                "image_endpoint": "http://127.0.0.1:8090/x",
                "chain_continuity": True,
                "anchor_image": True,
                "video_image_endpoint": "http://x/y",
            }
        )
        for removed in (
            "image_provider", "image_endpoint", "image_api_key", "ip_scale", "ip_face",
            "image_seed", "shot_first_frame", "auto_engine_switch",
            "image_switch_start_cmd", "image_switch_stop_cmd",
            "chain_continuity", "anchor_image", "video_image_endpoint",
        ):
            self.assertFalse(hasattr(cfg, removed), f"Config 不应再有字段 {removed}")

    def test_secret_fields_are_only_llm_and_video(self) -> None:
        """API Key 复用 video_api_key（不新增 rh_api_key）；脱敏字段仅 llm/video。"""
        self.assertEqual(ConfigStore._SECRET_FIELDS, ("llm_api_key", "video_api_key"))
        self.assertFalse(hasattr(Config(), "rh_api_key"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
