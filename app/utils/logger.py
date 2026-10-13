"""统一日志：控制台 + ``logs/app.log``，格式 ``[时间] [级别] [模块] 消息``。

同一格式的日志行同时可追加到 ``TaskStatus.logs``（由 TaskManager 负责）。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app.utils.paths import Paths

__all__ = ["get_logger", "format_task_log", "LOG_FORMAT"]

LOG_FORMAT = "[%(asctime)s] [%(levelname)-5s] [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured: set[str] = set()


def get_logger(name: str = "storyforge") -> logging.Logger:
    """获取（并首次配置）一个统一格式的日志器。

    Args:
        name: 日志器名称，对应格式中的 ``[模块]`` 部分。

    Returns:
        已挂载控制台 + 文件 handler 的 ``logging.Logger``。
    """
    logger = logging.getLogger(name)
    if name in _configured:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    try:
        Paths.ensure_runtime_dirs()
        file_handler = RotatingFileHandler(
            Paths.LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception:  # 文件系统异常时仍保留控制台输出，不影响主流程
        pass

    logger.propagate = False
    _configured.add(name)
    return logger


def format_task_log(message: str, *, level: str = "INFO") -> str:
    """格式化一条供前端「实时日志」区展示的行。

    Args:
        message: 日志正文（中文可读）。
        level: 级别文本，默认 ``INFO``。

    Returns:
        形如 ``[15:30:12] 已生成脚本，共 4 个镜头`` 的字符串。
    """
    from datetime import datetime

    stamp = datetime.now().strftime("%H:%M:%S")
    return f"[{stamp}] {message}"
