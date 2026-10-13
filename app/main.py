"""StoryForge FastAPI 应用入口。

职责：
- 挂载三个路由（config / pipeline / result）与静态目录 ``app/static``；
- 统一异常兜底（不向前端泄漏 traceback）；
- 启动事件：创建运行时目录、加载配置、打印实际访问地址；
- 端口探测：8000 起，被占用自动 +1；实际端口写入 ``logs/port.txt`` 并自动打开浏览器。
"""

from __future__ import annotations

import os
import platform
import socket
import threading
import time
import webbrowser
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __description__, __version__
from app.deps import get_config_store
from app.routers import config_router, pipeline_router, result_router
from app.utils.ffmpeg_utils import probe_ffmpeg
from app.utils.logger import get_logger
from app.utils.paths import Paths
from app.utils.responses import ErrorCode, err_msg, fail, ok

__all__ = ["app", "run", "find_free_port"]

_log = get_logger("server")

DEFAULT_PORT = 8000
DEFAULT_HOST = "127.0.0.1"
MAX_PORT_TRIES = 30


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时准备运行时目录与配置。

    Args:
        app: FastAPI 实例。
    """
    Paths.ensure_runtime_dirs()
    store = get_config_store()
    cfg = store.get_raw()
    ffmpeg_info = probe_ffmpeg()
    _log.info(
        "StoryForge v%s 启动完成 | LLM=%s | 视频=%s | FFmpeg=%s",
        __version__,
        cfg.llm_mode.value,
        cfg.video_provider.value,
        "可用" if ffmpeg_info["available"] else "缺失",
    )
    if not ffmpeg_info["available"]:
        _log.warning("FFmpeg 不可用：%s", ffmpeg_info["message"])
    yield
    _log.info("StoryForge 服务已停止")


app = FastAPI(
    title="StoryForge",
    description=__description__,
    version=__version__,
    lifespan=lifespan,
)
app.state.port = DEFAULT_PORT

# --------------------------------------------------------------------------- #
# 路由挂载
# --------------------------------------------------------------------------- #
app.include_router(config_router)
app.include_router(pipeline_router)
app.include_router(result_router)

if Paths.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(Paths.STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """返回单页应用首页（零外网依赖）。"""
    return FileResponse(str(Paths.STATIC_DIR / "index.html"))


@app.get("/api/health")
def health() -> dict:
    """返回启动自检结果。"""
    ffmpeg_info = probe_ffmpeg()
    return ok(
        {
            "status": "ok",
            "port": getattr(app.state, "port", DEFAULT_PORT),
            "ffmpeg": ffmpeg_info["available"],
            "ffmpeg_message": ffmpeg_info["message"],
            "ffmpeg_version": ffmpeg_info.get("version", ""),
            "python": platform.python_version(),
            "version": __version__,
        }
    )


# --------------------------------------------------------------------------- #
# 统一异常处理：不向前端泄漏 traceback
# --------------------------------------------------------------------------- #
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc: StarletteHTTPException) -> JSONResponse:
    """把 HTTPException 包装为统一响应格式。"""
    return JSONResponse(status_code=exc.status_code, content=fail(str(exc.detail)))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError) -> JSONResponse:
    """把 Pydantic 校验错误包装为中文可读的统一响应。"""
    details: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", []) if part != "body")
        details.append(f"{location or '参数'}: {error.get('msg', '非法值')}")
    message = "参数校验失败 - " + "；".join(details)
    return JSONResponse(status_code=422, content=fail(err_msg(ErrorCode.CFG_INVALID, message)))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    """兜底：记录完整堆栈到日志，仅向前端返回通用中文错误。"""
    _log.exception("未处理异常：%s", exc)
    return JSONResponse(
        status_code=500,
        content=fail(err_msg(ErrorCode.SYS_INTERNAL, "服务器内部错误，请查看 logs/app.log")),
    )


# --------------------------------------------------------------------------- #
# 端口探测与启动
# --------------------------------------------------------------------------- #
def find_free_port(start: int = DEFAULT_PORT, host: str = DEFAULT_HOST, tries: int = MAX_PORT_TRIES) -> int:
    """从 ``start`` 起探测第一个可用端口。

    Args:
        start: 起始端口。
        host: 绑定的主机地址。
        tries: 最多尝试的端口数量。

    Returns:
        可用端口号。

    Raises:
        RuntimeError: 指定范围内无可用端口。
    """
    for offset in range(tries):
        port = start + offset
        # 有意不设置 SO_REUSEADDR：在 Windows 上它会允许「bind 已被占用的端口」，
        # 从而把已占用端口误判为空闲（与 Linux 语义不同）。裸 bind 才能正确探测占用；
        # 捕获 OSError 即视为被占用并 +1 重试。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"{start}–{start + tries - 1} 范围内无可用端口，请释放端口后重试")


def _open_browser_later(url: str, delay: float = 1.5) -> None:
    """在后台线程中延迟打开浏览器。"""

    def _worker() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # 打开浏览器失败不影响服务
            pass

    threading.Thread(target=_worker, daemon=True).start()


def run(host: str = DEFAULT_HOST, port_start: int = DEFAULT_PORT) -> int:
    """探测端口并启动服务（供 ``start.bat`` / ``python -m app.main`` 调用）。

    Args:
        host: 绑定主机。
        port_start: 起始端口。

    Returns:
        实际监听的端口号。
    """
    Paths.ensure_runtime_dirs()
    port = find_free_port(port_start, host)
    app.state.port = port

    url = f"http://{host}:{port}"
    try:
        Paths.PORT_FILE.write_text(str(port), encoding="utf-8")
    except Exception:  # 写端口文件失败不影响启动
        pass

    _log.info("=" * 56)
    _log.info("StoryForge 已启动，请在浏览器访问：%s", url)
    _log.info("=" * 56)

    if os.environ.get("STORYFORGE_NO_BROWSER") != "1":
        _open_browser_later(url)

    uvicorn.run(app, host=host, port=port, log_level="info")
    return port


if __name__ == "__main__":
    run()
