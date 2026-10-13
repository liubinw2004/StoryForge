"""配置相关路由：``/api/config`` 读写、连接测试、Ollama 模型探测。"""

from __future__ import annotations

from fastapi import APIRouter

from app.deps import get_config_store
from app.models.schemas import Config
from app.services.llm_client import LLMError, OllamaClient, build_llm_client
from app.utils.logger import get_logger
from app.utils.responses import ErrorCode, err_msg, fail, ok

__all__ = ["router"]

router = APIRouter(prefix="/api/config", tags=["config"])
_log = get_logger("config")


@router.get("")
def get_config() -> dict:
    """读取当前配置（api_key 已脱敏）。"""
    return ok(get_config_store().get_public().model_dump(mode="json"))


@router.post("")
def save_config(cfg: Config) -> dict:
    """保存配置；若收到脱敏 api_key 则保留磁盘上的原值。"""
    try:
        saved = get_config_store().save(cfg)
    except Exception as exc:
        _log.error("配置保存失败：%s", exc)
        return fail(err_msg(ErrorCode.CFG_SAVE_FAILED, f"配置保存失败：{exc}"))
    return ok(saved.model_dump(mode="json"))


@router.post("/test-llm")
def test_llm(cfg: Config) -> dict:
    """测试 LLM 连通性（Ollama 走 ``/api/tags``，OpenAI 走 ``/v1/models``）。

    外层响应 ``ok`` 恒为 True（请求本身成功），连通结果见 ``data.ok``。
    """
    merged = get_config_store().merge_secrets(cfg)
    try:
        client = build_llm_client(merged)
        result = client.test_connection()
    except LLMError as exc:
        result = {"ok": False, "models": [], "latency_ms": 0, "message": str(exc)}
    except Exception as exc:
        result = {
            "ok": False,
            "models": [],
            "latency_ms": 0,
            "message": err_msg(ErrorCode.LLM_REQUEST_FAILED, str(exc)),
        }
    return ok(result)


@router.post("/test-video")
def test_video(cfg: Config) -> dict:
    """测试视频接口连通性（Mock 恒成功；RunningHub 工作流探活）。"""
    merged = get_config_store().merge_secrets(cfg)
    try:
        from app.services.video_generator import VideoGenerator

        result = VideoGenerator(merged).test_connection()
    except Exception as exc:
        result = {
            "ok": False,
            "provider": merged.video_provider.value,
            "message": err_msg(ErrorCode.VID_ADAPTER_MISSING, str(exc)),
        }
    return ok(result)


@router.get("/ollama-models")
def ollama_models() -> dict:
    """探测本地 Ollama 并列出模型（失败不阻塞，返回 ``detected: false``）。"""
    cfg = get_config_store().get_raw()
    base_url = cfg.llm_base_url or "http://localhost:11434"
    client = OllamaClient(base_url=base_url, model=cfg.llm_model)
    try:
        models = client.list_models()
        return ok({"detected": True, "models": models, "base_url": base_url, "message": ""})
    except LLMError as exc:
        return ok({"detected": False, "models": [], "base_url": base_url, "message": str(exc)})
    except Exception as exc:
        return ok(
            {
                "detected": False,
                "models": [],
                "base_url": base_url,
                "message": err_msg(ErrorCode.LLM_UNREACHABLE, str(exc)),
            }
        )
