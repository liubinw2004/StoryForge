"""统一 API 响应包装与错误码常量。

所有 JSON 响应统一为 ``{ "ok": bool, "data": any, "error": str|null }``。
错误信息建议携带错误码前缀，如 ``"LLM_UNREACHABLE: 无法连接 …"``。
"""

from __future__ import annotations

from typing import Any

__all__ = ["ok", "fail", "err_msg", "ErrorCode"]


def ok(data: Any = None) -> dict[str, Any]:
    """构造成功响应。

    Args:
        data: 业务数据（可为 None）。

    Returns:
        ``{"ok": True, "data": data, "error": None}``
    """
    return {"ok": True, "data": data, "error": None}


def fail(error: str, data: Any = None) -> dict[str, Any]:
    """构造失败响应。

    Args:
        error: 中文可读错误信息（建议带错误码前缀）。
        data: 可选的附加数据。

    Returns:
        ``{"ok": False, "data": data, "error": error}``
    """
    return {"ok": False, "data": data, "error": error}


def err_msg(code: str, message: str) -> str:
    """拼接 ``错误码: 中文信息`` 形式的错误串。"""
    return f"{code}: {message}"


class ErrorCode:
    """统一错误码前缀常量（见 ARCHITECTURE §7.2）。"""

    # 配置
    CFG_INVALID = "CFG_INVALID"
    CFG_SAVE_FAILED = "CFG_SAVE_FAILED"
    CFG_NOT_FOUND = "CFG_NOT_FOUND"

    # LLM
    LLM_UNAUTHORIZED = "LLM_UNAUTHORIZED"
    LLM_MODEL_NOT_FOUND = "LLM_MODEL_NOT_FOUND"
    LLM_UNREACHABLE = "LLM_UNREACHABLE"
    LLM_BAD_JSON = "LLM_BAD_JSON"
    LLM_REQUEST_FAILED = "LLM_REQUEST_FAILED"
    # 模型处于思考模式、正文被思考内容挤空（content 空但 reasoning 非空）
    LLM_CONTENT_EMPTY = "LLM_CONTENT_EMPTY"

    # 视频
    VID_ADAPTER_MISSING = "VID_ADAPTER_MISSING"
    VID_API_ERROR = "VID_API_ERROR"
    VID_NOT_CONFIGURED = "VID_NOT_CONFIGURED"
    VID_GENERATE_FAILED = "VID_GENERATE_FAILED"
    VID_INVALID_PARAM = "VID_INVALID_PARAM"

    # FFmpeg
    FFM_NOT_FOUND = "FFM_NOT_FOUND"
    FFM_EXEC_FAILED = "FFM_EXEC_FAILED"
    FFM_CONCAT_FAILED = "FFM_CONCAT_FAILED"

    # 任务
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_BUSY = "TASK_BUSY"
    TASK_FAILED = "TASK_FAILED"
    TASK_STATE_INVALID = "TASK_STATE_INVALID"  # 增量：任务不在待确认状态，无法确认续跑

    # 系统
    SYS_INTERNAL = "SYS_INTERNAL"
    SYS_BAD_REQUEST = "SYS_BAD_REQUEST"
