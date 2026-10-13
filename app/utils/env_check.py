"""启动环境自检：Python 版本 / 关键依赖 / FFmpeg 可用性。

所有函数**不抛异常**，返回带中文诊断信息的字典，供 ``/api/health`` 与
``start.bat`` 展示。
"""

from __future__ import annotations

import importlib
import platform
import sys

__all__ = [
    "check_python",
    "check_dependencies",
    "check_ffmpeg",
    "run_all_checks",
]

_REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("pydantic", "pydantic"),
    ("httpx", "httpx"),
    ("openai", "openai"),
)


def check_python() -> dict:
    """检查 Python 版本（要求 ≥ 3.10，推荐 3.13）。

    Returns:
        ``{"ok": bool, "version": str, "message": str}``
    """
    version = platform.python_version()
    ok = sys.version_info >= (3, 10)
    if ok:
        message = f"Python {version} 可用"
    else:
        message = f"Python {version} 版本过低，请升级到 3.10 及以上（推荐 3.13）"
    return {"ok": ok, "version": version, "message": message}


def check_dependencies() -> dict:
    """检查关键第三方依赖是否已安装。

    Returns:
        ``{"ok": bool, "missing": [str], "message": str}``
    """
    missing: list[str] = []
    for module_name, package_name in _REQUIRED_PACKAGES:
        try:
            importlib.import_module(module_name)
        except Exception:
            missing.append(package_name)
    if missing:
        message = "缺少依赖：" + "、".join(missing) + "，请运行 pip install -r requirements.txt"
        return {"ok": False, "missing": missing, "message": message}
    return {"ok": True, "missing": [], "message": "依赖完整"}


def check_ffmpeg() -> dict:
    """检查 FFmpeg 是否可用（委托给 ffmpeg_utils，避免重复实现）。

    Returns:
        ``{"available": bool, "version": str, "path": str, "message": str}``
    """
    from app.utils.ffmpeg_utils import probe_ffmpeg

    return probe_ffmpeg()


def run_all_checks() -> dict:
    """一次性执行全部自检。

    Returns:
        ``{"python": {...}, "dependencies": {...}, "ffmpeg": {...}, "ok": bool}``
    """
    python_info = check_python()
    deps_info = check_dependencies()
    ffmpeg_info = check_ffmpeg()
    overall = bool(python_info["ok"] and deps_info["ok"] and ffmpeg_info["available"])
    return {
        "python": python_info,
        "dependencies": deps_info,
        "ffmpeg": ffmpeg_info,
        "ok": overall,
    }
