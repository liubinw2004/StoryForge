"""可复现构建脚本：用 PyInstaller 生成「解压即用」的 StoryForge 便携包。

产物：
- ``dist/StoryForge/StoryForge.exe``（onedir，含 ``_internal/``）
- ``dist/StoryForge/bin/ffmpeg.exe``、``bin/ffprobe.exe``（内嵌，免用户自备）
- ``dist/StoryForge/使用说明-便携版.txt``（UTF-8 with BOM，记事本可正常显示）
- ``dist/StoryForge-portable-v1.0.zip``

用法：
    .venv\\Scripts\\python.exe scripts\\build_exe.py

设计要点：
- 使用 ``--onedir``（更稳、启动更快、静态资源更好处理）；
- ``--add-data "app/static;app/static"`` 把前端静态资源打进包；
- 补齐 uvicorn / pydantic 动态导入所需的 hidden imports 与 collect；
- 构建后把本机 ffmpeg/ffprobe 复制进包内 ``bin/``；
- 若 PyInstaller 在当前 Python 版本上不可用，脚本会给出明确指引（降级见 README）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
APP_NAME = "StoryForge"
VERSION = "1.0.0"

# 本机 FFmpeg 8.1（Gyan build）默认位置；可通过环境变量 FFMPEG_BIN_DIR 覆盖。
_DEFAULT_FFMPEG_DIR = (
    r"C:\Users\31706\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
)

# uvicorn / 生态的动态导入（缺失会导致冻结后启动即崩）
HIDDEN_IMPORTS: tuple[str, ...] = (
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
)

# 需要整包收集（pydantic v2 的编译核心与 fastapi 的元数据）
COLLECT_ALL: tuple[str, ...] = (
    "pydantic",
    "pydantic_core",
    "uvicorn",
)

README_NAME = f"使用说明-便携版.txt"

_README_TEXT = f"""StoryForge 便携版 v{VERSION} 使用说明
====================================

一、这是什么
  这是「解压即用」的 StoryForge 便携版：无需安装 Python，也无需自备 FFmpeg。

二、如何运行
  1. 把整个 {APP_NAME} 文件夹解压到任意目录（建议放在非系统盘，例如 D:\\{APP_NAME}）。
  2. 双击文件夹内的 {APP_NAME}.exe。
  3. 稍等几秒，默认浏览器会自动打开 http://127.0.0.1:8000 界面。
  4. 关闭那个黑色命令行窗口即停止服务。

三、首次运行会生成什么
  在 {APP_NAME}.exe 同级目录会自动创建：
    config\\config.json   应用配置
    outputs\\             生成的分镜视频与成片
    logs\\app.log         运行日志
  这些目录可安全删除（删除后下次运行会重新生成默认值）。

四、关于 FFmpeg
  本包已内嵌 bin\\ffmpeg.exe 与 bin\\ffprobe.exe，无需另行安装。
  如界面提示 FFmpeg 不可用，请确认 bin\\ 目录存在且文件未被杀毒软件隔离。

五、常见问题
  Q: 双击后窗口一闪而过？
  A: 通常是被杀毒软件拦截或文件未完整解压。请先完整解压，再右键「以管理员身份运行」重试。
  Q: 端口被占用？
  A: 程序会自动从 8000 起向后寻找空闲端口，浏览器打开的即为实际地址。
  Q: 如何接入真实大模型 / 视频服务？
  A: 打开界面中的「配置」页填写，或直接编辑 config\\config.json。

六、数据安全
  本程序完全在本机运行，除你在配置中指定的 LLM / 视频服务外，不向任何第三方上传数据。
"""


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    """执行命令并在失败时抛出 ``RuntimeError``。"""
    printable = " ".join(cmd)
    print(f"[build] $ {printable}", flush=True)
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if result.returncode != 0:
        raise RuntimeError(f"命令失败（退出码 {result.returncode}）：{printable}")


def ensure_pyinstaller(python: str) -> None:
    """确保 PyInstaller 可用，否则自动安装。"""
    check = subprocess.run(
        [python, "-c", "import PyInstaller; print(PyInstaller.__version__)"],
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        print(f"[build] PyInstaller 已就绪：{check.stdout.strip()}", flush=True)
        return
    print("[build] 未检测到 PyInstaller，正在安装 ...", flush=True)
    _run([python, "-m", "pip", "install", "--upgrade", "pyinstaller"])


def clean_previous() -> None:
    """清理上一次构建产物（幂等）。"""
    for target in (DIST_DIR / APP_NAME, BUILD_DIR / APP_NAME):
        shutil.rmtree(target, ignore_errors=True)


def build_with_pyinstaller(python: str) -> Path:
    """调用 PyInstaller 生成 onedir 包，返回可执行文件所在目录。"""
    cmd = [
        python, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir",
        "--name", APP_NAME,
        "--console",
        "--add-data", "app/static;app/static",
    ]
    for name in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", name]
    for package in COLLECT_ALL:
        cmd += ["--collect-all", package]
    cmd.append("run_storyforge.py")

    _run(cmd, cwd=PROJECT_ROOT)

    exe_dir = DIST_DIR / APP_NAME
    if not exe_dir.is_dir():
        raise RuntimeError(f"未找到构建输出目录：{exe_dir}")
    return exe_dir


def _locate_ffmpeg_source() -> Path:
    """定位本机 FFmpeg 的 bin 目录（环境变量优先，其次默认位置，最后 PATH）。"""
    override = os.environ.get("FFMPEG_BIN_DIR", "").strip()
    candidates = []
    if override:
        candidates.append(Path(override))
    candidates.append(Path(_DEFAULT_FFMPEG_DIR))

    for candidate in candidates:
        if (candidate / "ffmpeg.exe").is_file() and (candidate / "ffprobe.exe").is_file():
            return candidate
    for name in ("ffmpeg", "ffprobe"):
        located = shutil.which(name)
        if located:
            return Path(located).resolve().parent
    raise RuntimeError(
        "未找到 ffmpeg.exe / ffprobe.exe；请设置环境变量 FFMPEG_BIN_DIR 指向其所在目录"
    )


def copy_ffmpeg(exe_dir: Path) -> list[str]:
    """把 ffmpeg/ffprobe 复制进包内 ``bin/``，返回已复制的文件名列表。"""
    source = _locate_ffmpeg_source()
    bin_dir = exe_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        src = source / name
        if not src.is_file():
            raise RuntimeError(f"缺少源文件：{src}")
        shutil.copy2(src, bin_dir / name)
        copied.append(name)
        print(f"[build] 已内嵌 {name} -> {bin_dir / name}", flush=True)
    return copied


def write_readme(exe_dir: Path) -> Path:
    """写入 UTF-8 with BOM 的中文使用说明。"""
    target = exe_dir / README_NAME
    target.write_text(_README_TEXT, encoding="utf-8-sig")
    print(f"[build] 已写入说明文件：{target}", flush=True)
    return target


def make_zip(exe_dir: Path) -> Path:
    """把包目录打包为 zip（保持相对路径，便于解压即用）。"""
    zip_path = DIST_DIR / f"{APP_NAME}-portable-v{VERSION}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(exe_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(DIST_DIR).as_posix())
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"[build] 已生成压缩包：{zip_path}（{size_mb:.1f} MB）", flush=True)
    return zip_path


def main() -> int:
    """构建主流程。"""
    python = sys.executable
    print(f"[build] 使用 Python：{python}", flush=True)
    print(f"[build] 项目根：{PROJECT_ROOT}", flush=True)

    ensure_pyinstaller(python)
    clean_previous()
    exe_dir = build_with_pyinstaller(python)
    copy_ffmpeg(exe_dir)
    write_readme(exe_dir)
    zip_path = make_zip(exe_dir)

    print()
    print("[build] 构建完成 🎉")
    print(f"[build] 可执行目录：{exe_dir}")
    print(f"[build] 发布压缩包：{zip_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
