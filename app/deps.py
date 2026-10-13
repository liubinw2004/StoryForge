"""依赖注入：全局单例的 ConfigStore / TaskManager 与 LLM 客户端工厂。"""

from __future__ import annotations

import threading

from app.models.schemas import Config
from app.services.config_store import ConfigStore
from app.services.llm_client import LLMClient, build_llm_client

__all__ = [
    "get_config_store",
    "get_task_manager",
    "get_llm_client",
    "reset_singletons",
]

_lock = threading.RLock()
_config_store: ConfigStore | None = None
_task_manager = None  # type: ignore[assignment]  # 延迟导入，避免循环依赖


def get_config_store() -> ConfigStore:
    """返回全局唯一的 :class:`ConfigStore` 单例。"""
    global _config_store
    with _lock:
        if _config_store is None:
            _config_store = ConfigStore()
        return _config_store


def get_task_manager():
    """返回全局唯一的 :class:`TaskManager` 单例（延迟导入）。"""
    global _task_manager
    with _lock:
        if _task_manager is None:
            from app.services.task_manager import TaskManager

            _task_manager = TaskManager()
        return _task_manager


def get_llm_client(cfg: Config | None = None) -> LLMClient:
    """基于（当前或指定）配置构造 LLM 客户端。

    Args:
        cfg: 配置对象；为 None 时读取当前持久化配置（含明文 key）。

    Returns:
        LLM 客户端实例。
    """
    effective = cfg if cfg is not None else get_config_store().get_raw()
    return build_llm_client(effective)


def reset_singletons() -> None:
    """重置单例（仅测试使用）。"""
    global _config_store, _task_manager
    with _lock:
        _config_store = None
        _task_manager = None
