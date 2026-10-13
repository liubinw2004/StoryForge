"""StoryForge 启动器（承载全部中文 UX 逻辑）。

由纯 ASCII 的 ``start.bat`` 调用，避免中文出现在 bat 中导致 cmd 按 GBK 误解析、
破坏命令行分词。职责：

1. 打印中文欢迎信息与分步状态；
2. 复用 ``app.utils.env_check`` / ``app.utils.ffmpeg_utils`` 做环境自检；
3. 复用 ``app.main.run()`` 启动服务（自动探测端口并打开浏览器）；
4. 任何失败给出**中文可读指引**并等待用户回车，窗口不闪退。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# --------------------------------------------------------------------------- #
# 保证可从任意工作目录导入项目包（双击 / 命令行均可）
# --------------------------------------------------------------------------- #
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
try:
    os.chdir(_PROJECT_ROOT)
except OSError:
    pass


def _setup_output_encoding() -> None:
    """尽量让中文在控制台/重定向场景下都不乱码。

    Windows 交互式控制台在 Python 3.6+ 本身即按 UTF-8 处理并直接调用
    ``WriteConsoleW``，因此重设为 UTF-8 是安全且正确的；重定向到文件时同样
    以 UTF-8 落盘。``errors="replace"`` 保证极端情况下也不会抛异常。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 某些被包装的流不支持 reconfigure
            pass


def _rule(char: str = "=", width: int = 60) -> str:
    """构造一条分隔线。"""
    return char * width


def _pause(message: str = "按回车键退出...") -> None:
    """等待用户回车（非交互环境不阻塞）。"""
    try:
        input(message)
    except (EOFError, KeyboardInterrupt):
        pass


def _print_banner() -> None:
    """打印中文欢迎横幅。"""
    print(_rule())
    print("   StoryForge - AI 短剧生成工作台")
    print(_rule())
    print()


def _report_environment() -> dict:
    """执行环境自检并打印中文结果。

    Returns:
        :func:`app.utils.env_check.run_all_checks` 的返回字典。
    """
    from app.utils.env_check import run_all_checks

    result = run_all_checks()

    python_info = result["python"]
    print(f"[1/3] {python_info['message']}")

    deps_info = result["dependencies"]
    if deps_info["ok"]:
        print("[2/3] 依赖完整")
    else:
        print(f"[2/3] [警告] {deps_info['message']}")

    ffmpeg_info = result["ffmpeg"]
    if ffmpeg_info["available"]:
        print(f"[3/3] {ffmpeg_info['message']}")
    else:
        print(f"[3/3] [警告] {ffmpeg_info['message']}")
        print("        Mock 视频生成与拼接将不可用。请下载 FFmpeg 并加入 PATH：")
        print("        https://www.gyan.dev/ffmpeg/builds/")
    print()
    return result


def main() -> int:
    """启动器主流程。

    Returns:
        进程退出码（0 成功 / 非 0 失败）。
    """
    _setup_output_encoding()
    _print_banner()

    checks = _report_environment()

    if not checks["python"]["ok"]:
        print("[错误] Python 版本过低，无法运行 StoryForge。")
        print("       请安装 Python 3.10 及以上版本（推荐 3.13）：")
        print("       https://www.python.org/downloads/")
        print()
        _pause()
        return 1

    if not checks["dependencies"]["ok"]:
        print("[错误] 缺少必要依赖，无法启动。请在本项目目录执行：")
        print(r'       ".venv\Scripts\python.exe" -m pip install -r requirements.txt')
        print()
        _pause()
        return 1

    if not checks["ffmpeg"]["available"]:
        print("[提示] 未检测到 FFmpeg：界面仍可打开，但生成 Mock 视频会失败。")
        print()

    print(_rule("-"))
    print("正在启动 StoryForge 服务，浏览器将自动打开实际地址...")
    print("本窗口关闭即停止服务。")
    print(_rule("-"))
    print()

    try:
        # 默认允许自动打开浏览器（=1 时禁用，供自动化测试使用）
        os.environ.setdefault("STORYFORGE_NO_BROWSER", "0")

        from app.main import run

        port = run()
        print()
        print(f"服务已停止（实际端口：{port}）。")
        _pause()
        return 0
    except KeyboardInterrupt:
        print()
        print("已按 Ctrl+C，StoryForge 服务已停止。")
        _pause()
        return 0
    except Exception as exc:  # noqa: BLE001 - 兜底展示，避免窗口闪退
        print()
        print(_rule("!"))
        print("[错误] StoryForge 启动失败，无法继续运行。")
        print(f"       原因：{exc}")
        print("       排查建议：")
        print("         1) 确认本机 8000-8029 端口未全部被占用；")
        print(r"         2) 查看 logs\app.log 了解详细堆栈；")
        print("         3) 重新双击 start.bat 重试。")
        print(_rule("!"))
        print()
        traceback.print_exc()
        _pause()
        return 1


if __name__ == "__main__":
    sys.exit(main())
