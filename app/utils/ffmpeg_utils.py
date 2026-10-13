"""FFmpeg 命令行封装：Mock 占位视频、抽末帧、视频拼接、时长探测。

设计要点（见 ARCHITECTURE §7.4）：
- 全部通过 ``subprocess.run(list)`` 直接调用，**不经 shell**（不使用 ``shell=True``）；
- Windows 中文路径：参数以 list 形式传递，路径由 Python 拼接，filter 内路径加引号转义；
- 不引入 moviepy，纯命令行调用，稳定且无重依赖；
- 所有函数在失败时抛出 :class:`FFmpegError`（携带 ``FFM_*`` 错误码）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.utils.logger import get_logger
from app.utils.paths import Paths
from app.utils.responses import ErrorCode, err_msg

__all__ = [
    "FFmpegError",
    "probe_ffmpeg",
    "make_mock_video",
    "make_mock_image",
    "extract_last_frame",
    "extract_tail_frame",
    "concat_videos",
    "trim_clip",
    "export_from_edl",
    "probe_duration",
]

_log = get_logger("ffmpeg")

_IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000  # Windows 下隐藏子进程控制台窗口

# Windows / POSIX 常见字体候选（drawtext 需要 fontfile）
_FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/PingFang.ttc",
)


class FFmpegError(Exception):
    """FFmpeg 调用异常，携带 ``FFM_*`` 错误码。"""

    def __init__(self, code: str, message: str) -> None:
        """初始化异常。

        Args:
            code: 错误码（:class:`app.utils.responses.ErrorCode`）。
            message: 中文可读信息。
        """
        self.code = code
        self.message = message
        super().__init__(err_msg(code, message))


# --------------------------------------------------------------------------- #
# 二进制解析：包内 bin/ 优先，其次系统 PATH
# --------------------------------------------------------------------------- #
def _bundled_bin_dir() -> Path | None:
    """返回包内 ``bin/`` 目录（不存在则返回 ``None``）。

    冻结时 ``Paths.ROOT`` = exe 所在目录 → ``<exe_dir>/bin``；
    非冻结时 ``Paths.ROOT`` = 项目根 → ``<project_root>/bin``。
    两种情况下统一为 ``Paths.ROOT / "bin"``。
    """
    directory = Paths.ROOT / "bin"
    return directory if directory.is_dir() else None


def _bundled_exe(name: str) -> str | None:
    """在包内 ``bin/`` 中查找可执行文件（Windows 兼容 ``.exe`` 后缀）。

    Args:
        name: 基础名，如 ``"ffmpeg"`` / ``"ffprobe"``。

    Returns:
        命中的绝对路径；未命中返回 ``None``。
    """
    directory = _bundled_bin_dir()
    if directory is None:
        return None
    for suffix in (".exe", ""):
        candidate = directory / f"{name}{suffix}"
        if candidate.is_file():
            return str(candidate)
    return None


def _resolve_bin(name: str) -> str | None:
    """按「① 包内 ``bin/`` → ② 系统 PATH」顺序解析可执行文件。

    Args:
        name: 可执行文件基础名（如 ``ffmpeg`` / ``ffprobe``）。

    Returns:
        解析到的绝对路径；均未命中返回 ``None``。
    """
    return _bundled_exe(name) or shutil.which(name)


def _ffmpeg_bin() -> str:
    """返回 ffmpeg 可执行文件路径（包内 bin/ 优先，其次 PATH）。"""
    return _resolve_bin("ffmpeg") or "ffmpeg"


def _require_ffmpeg() -> str:
    """返回 ffmpeg 可执行文件路径；若二进制不存在/不可用则抛出 ``FFM_NOT_FOUND``。

    与 :func:`_ffmpeg_bin` 的区别：本函数在 FFmpeg 缺失时**提前、确定性地**抛出
    ``FFM_NOT_FOUND``，避免下游把「二进制缺失」误报为「执行失败」。

    Raises:
        FFmpegError: FFmpeg 既不在包内 bin/ 也不在 PATH 中。
    """
    exe = _resolve_bin("ffmpeg")
    if not exe:
        raise FFmpegError(
            ErrorCode.FFM_NOT_FOUND,
            "未找到 FFmpeg，请安装并加入 PATH（下载：https://www.gyan.dev/ffmpeg/builds/）",
        )
    return exe


def _ffprobe_bin() -> str | None:
    """返回 ffprobe 可执行文件路径（包内 bin/ 优先，可能为 None）。"""
    return _resolve_bin("ffprobe")


def _run(cmd: list[str], *, cwd: str | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    """以 subprocess.run 执行命令（不经 shell）。

    Args:
        cmd: 参数列表。
        cwd: 工作目录（用于让 filter 内的相对路径（如 textfile）免转义）。
        timeout: 超时秒数。

    Returns:
        ``CompletedProcess``。

    Raises:
        FFmpegError: 找不到可执行文件或执行超时。
    """
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if cwd:
        kwargs["cwd"] = str(cwd)
    if _IS_WINDOWS:
        kwargs["creationflags"] = _CREATE_NO_WINDOW

    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError as exc:
        raise FFmpegError(
            ErrorCode.FFM_NOT_FOUND,
            "未找到 FFmpeg，请安装并加入 PATH（下载：https://www.gyan.dev/ffmpeg/builds/）",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(ErrorCode.FFM_EXEC_FAILED, f"FFmpeg 执行超时（>{timeout}s）") from exc
    except OSError as exc:
        raise FFmpegError(ErrorCode.FFM_EXEC_FAILED, f"FFmpeg 启动失败：{exc}") from exc


def probe_ffmpeg() -> dict:
    """探测 FFmpeg 是否可用。

    Returns:
        ``{"available": bool, "version": str, "path": str, "message": str}``
    """
    exe = _resolve_bin("ffmpeg")
    if not exe:
        return {
            "available": False,
            "version": "",
            "path": "",
            "message": "未检测到 FFmpeg，请安装并加入 PATH（下载：https://www.gyan.dev/ffmpeg/builds/）",
        }
    try:
        proc = _run([exe, "-version"], timeout=15)
    except FFmpegError as exc:
        return {"available": False, "version": "", "path": exe, "message": str(exc)}

    stdout = proc.stdout or ""
    first_line = stdout.splitlines()[0] if stdout else ""
    if proc.returncode != 0 or not first_line:
        return {"available": False, "version": "", "path": exe, "message": "FFmpeg 版本探测失败"}

    version = ""
    match = re.search(r"ffmpeg version\s+(\S+)", first_line)
    if match:
        version = match.group(1)
    return {
        "available": True,
        "version": version,
        "path": exe,
        "message": f"FFmpeg 可用（{first_line[:70]}）",
    }


# --------------------------------------------------------------------------- #
# 辅助工具
# --------------------------------------------------------------------------- #
def _find_font() -> str | None:
    """探测系统可用字体文件（drawtext 需要）。找不到返回 None。"""
    for candidate in _FONT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def _filter_escape_path(path: str) -> str:
    """转义 filtergraph 中的路径（反斜杠转正斜杠、冒号转义）。"""
    return path.replace("\\", "/").replace(":", "\\:")


def _wrap_lines(lines: list[str], width: int) -> list[str]:
    """按字符宽度对文本做近似换行（对中英文混排足够）。

    Args:
        lines: 原始文本行。
        width: 每行最大字符数。

    Returns:
        换行后的文本行列表。
    """
    wrapped: list[str] = []
    for line in lines:
        text = (line or "").strip()
        if not text:
            continue
        while len(text) > width:
            wrapped.append(text[:width])
            text = text[width:]
        wrapped.append(text)
    return wrapped


# --------------------------------------------------------------------------- #
# Mock 占位视频
# --------------------------------------------------------------------------- #
def _build_mock_cmd(
    exe: str,
    out_path: str,
    *,
    font: str | None,
    use_gradient: bool,
    use_text: bool,
    body_line_count: int,
    duration: int,
    width: int,
    height: int,
    fps: int,
    color0: str,
    color1: str,
) -> list[str]:
    """构造 Mock 视频的 ffmpeg 命令（渐变背景 + 文字 + 镜头编号 + 进度条）。

    注意：drawtext 会把文本中的换行符渲染成一个乱码方块，因此**每行文本单独使用
    一个 drawtext + 单行 textfile**，不使用多行文本。
    """
    if use_gradient:
        # speed=0 -> 静态渐变（每个镜头一张固定底图，避免中途变色难以辨识）
        source = (
            f"gradients=size={width}x{height}:c0={color0}:c1={color1}"
            f":x0=0:y0=0:x1={width}:y1={height}:speed=0:duration={duration}:rate={fps}"
        )
    else:
        source = f"color=c={color0}:s={width}x{height}:d={duration}:r={fps}"

    filters: list[str] = []
    # 顶部深色信息条
    top_h = max(48, int(height * 0.18))
    filters.append(f"drawbox=x=0:y=0:w={width}:h={top_h}:color=black@0.38:t=fill")

    if use_text and font:
        escaped_font = _filter_escape_path(font)
        header_size = max(28, int(height * 0.075))
        body_size = max(18, int(height * 0.042))
        line_h = body_size + 12

        # 镜头编号（一行）
        filters.append(
            f"drawtext=fontfile='{escaped_font}':textfile='header.txt'"
            f":fontcolor=white:fontsize={header_size}:x=40:y={int(height * 0.04)}"
        )

        # 正文块：先画半透明底板，再逐行叠加文本（逐行避免换行符乱码）
        if body_line_count > 0:
            body_top = int(height * 0.30)
            panel_h = line_h * body_line_count + 28
            filters.append(
                f"drawbox=x=32:y={body_top - 14}:w={width - 64}:h={panel_h}"
                f":color=black@0.35:t=fill"
            )
            for index in range(body_line_count):
                filters.append(
                    f"drawtext=fontfile='{escaped_font}':textfile='body_{index}.txt'"
                    f":fontcolor=white:fontsize={body_size}"
                    f":x=48:y={body_top + index * line_h}"
                )

    # 底部进度条：深色轨道 + 随时间的绿色填充
    # 注意：drawbox 的 w 表达式不支持时间变量 t（只在初始化时求值一次），
    # 因此改用「分段 enable」的方式实现随时间增长的进度条（enable 支持 t，已实测有效）。
    bar_h = max(12, int(height * 0.03))
    bar_y = height - bar_h
    filters.append(f"drawbox=x=0:y={bar_y}:w={width}:h={bar_h}:color=black@0.5:t=fill")
    steps = max(10, min(40, int(duration) * 8))
    for step in range(steps):
        start = duration * step / steps
        end = duration * (step + 1) / steps
        step_width = max(1, int(width * (step + 1) / steps))
        filters.append(
            f"drawbox=x=0:y={bar_y}:w={step_width}:h={bar_h}"
            f":color=0x22C55E@0.95:t=fill:enable='between(t,{start:.3f},{end:.3f})'"
        )

    vf = ",".join(filters)
    return [
        exe,
        "-y",
        "-f",
        "lavfi",
        "-i",
        source,
        "-vf",
        vf,
        "-t",
        str(duration),
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        out_path,
    ]


def make_mock_video(
    out_path: str,
    *,
    header: str = "SHOT",
    body_lines: list[str] | None = None,
    duration: int = 4,
    width: int = 1280,
    height: int = 720,
    fps: int = 24,
    color0: str = "0x1E3A8A",
    color1: str = "0x9333EA",
    timeout: int = 240,
) -> str:
    """生成一个 Mock 占位视频（渐变背景 + 文字 + 镜头编号 + 进度条）。

    依次尝试若干降级方案，确保「哪怕没有可用字体」也能产出可播放的 mp4(H.264)：
    ① 渐变 + 文字；② 渐变无文字；③ 纯色 + 文字；④ 纯色无文字。

    Args:
        out_path: 输出 mp4 绝对路径。
        header: 顶部大字（如 ``SHOT 3 / 6``）。
        body_lines: 正文文本行（场景 / 对白 / 画面）。
        duration: 时长（秒）。
        width: 宽。
        height: 高。
        fps: 帧率。
        color0: 渐变起始色。
        color1: 渐变结束色。
        timeout: 超时秒数。

    Returns:
        落盘后的绝对路径。

    Raises:
        FFmpegError: 所有降级方案均失败。
    """
    out_abs = os.path.abspath(out_path)
    Path(out_abs).parent.mkdir(parents=True, exist_ok=True)
    duration = max(1, int(round(duration)))
    body_lines = body_lines or []
    exe = _require_ffmpeg()

    workdir = tempfile.mkdtemp(prefix="storyforge_mock_")
    try:
        # 用 textfile 传文本，避免 filtergraph 内文本转义问题；相对文件名 + cwd 免路径转义。
        # 关键：**每行写一个独立的单行文件**——drawtext 会把多行文本里的换行符渲染成乱码方块。
        (Path(workdir) / "header.txt").write_text(
            (header or "SHOT").replace("\n", " ").strip(), encoding="utf-8"
        )
        wrapped = _wrap_lines(body_lines, 24)
        for index, line in enumerate(wrapped):
            (Path(workdir) / f"body_{index}.txt").write_text(line, encoding="utf-8")

        font = _find_font()
        if font is None:
            _log.warning("未找到可用字体，Mock 视频将降级为不含文字（仅渐变 + 进度条）")

        last_error = ""
        attempts = ((True, True), (True, False), (False, True), (False, False))
        for use_gradient, use_text in attempts:
            cmd = _build_mock_cmd(
                exe,
                out_abs,
                font=font,
                use_gradient=use_gradient,
                use_text=use_text,
                body_line_count=len(wrapped) if use_text else 0,
                duration=duration,
                width=width,
                height=height,
                fps=fps,
                color0=color0,
                color1=color1,
            )
            try:
                proc = _run(cmd, cwd=workdir, timeout=timeout)
            except FFmpegError as exc:
                last_error = str(exc)
                if exc.code == ErrorCode.FFM_NOT_FOUND:
                    # 二进制缺失/不可执行：继续降级重试没有意义，直接上抛 FFM_NOT_FOUND
                    raise
                continue
            if proc.returncode == 0 and os.path.exists(out_abs) and os.path.getsize(out_abs) > 0:
                _log.info(
                    "Mock 视频已生成：%s（%sx%s, %ss, 渐变=%s, 文字=%s）",
                    os.path.basename(out_abs),
                    width,
                    height,
                    duration,
                    use_gradient,
                    use_text,
                )
                return out_abs
            last_error = (proc.stderr or "")[-400:]
            _log.warning("Mock 视频方案 [渐变=%s 文字=%s] 失败，尝试降级…", use_gradient, use_text)

        raise FFmpegError(ErrorCode.FFM_EXEC_FAILED, f"Mock 视频生成失败：{last_error}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Mock 占位图片（场景开场帧）
# --------------------------------------------------------------------------- #
def make_mock_image(
    out_path: str,
    header: str = "",
    width: int = 1280,
    height: int = 720,
    *,
    color: str = "0x334155",
    timeout: int = 120,
) -> str:
    """生成一张纯色占位 JPG（可选叠加居中文字），用于 Mock 模式的场景开场帧。

    零成本、纯本地（ffmpeg lavfi），与 :func:`make_mock_video` 同一套降级思想：
    ① 纯色 + 文字（有字体时）；② 纯色无文字。**绝不访问网络**。

    Args:
        out_path: 输出 jpg 绝对路径。
        header: 叠加在画面中央的文字（空则不加）。
        width: 宽。
        height: 高。
        color: 底色（ffmpeg 颜色字面量，如 ``0x334155``）。
        timeout: 超时秒数。

    Returns:
        落盘后的绝对路径。

    Raises:
        FFmpegError: 所有降级方案均失败。
    """
    out_abs = os.path.abspath(out_path)
    Path(out_abs).parent.mkdir(parents=True, exist_ok=True)
    exe = _require_ffmpeg()

    workdir = tempfile.mkdtemp(prefix="storyforge_mockimg_")
    try:
        font = _find_font()
        header_text = (header or "").replace("\n", " ").strip()

        source = f"color=c={color}:s={width}x{height}"
        attempts: list[tuple[bool, str]] = []
        if header_text and font:
            (Path(workdir) / "header.txt").write_text(header_text, encoding="utf-8")
            escaped_font = _filter_escape_path(font)
            font_size = max(24, int(height * 0.08))
            vf = (
                f"drawtext=fontfile='{escaped_font}':textfile='header.txt'"
                f":fontcolor=white:fontsize={font_size}:x=(w-text_w)/2:y=(h-text_h)/2"
            )
            attempts.append((True, vf))
        attempts.append((False, ""))

        last_error = ""
        for use_text, vf in attempts:
            cmd = [exe, "-y", "-f", "lavfi", "-i", source]
            if vf:
                cmd += ["-vf", vf]
            cmd += ["-frames:v", "1", "-q:v", "3", out_abs]
            try:
                proc = _run(cmd, cwd=workdir, timeout=timeout)
            except FFmpegError as exc:
                last_error = str(exc)
                if exc.code == ErrorCode.FFM_NOT_FOUND:
                    raise
                continue
            if proc.returncode == 0 and os.path.exists(out_abs) and os.path.getsize(out_abs) > 0:
                _log.info(
                    "Mock 开场帧已生成：%s（%sx%s, 文字=%s）",
                    os.path.basename(out_abs),
                    width,
                    height,
                    use_text,
                )
                return out_abs
            last_error = (proc.stderr or "")[-400:]
            _log.warning("Mock 开场帧方案 [文字=%s] 失败，尝试降级…", use_text)

        raise FFmpegError(ErrorCode.FFM_EXEC_FAILED, f"Mock 开场帧生成失败：{last_error}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 抽末帧
# --------------------------------------------------------------------------- #
def extract_last_frame(video_path: str, out_jpg: str) -> str:
    """从视频抽取最后一帧为 JPG（用于视觉连续性参考）。

    Args:
        video_path: 源视频路径。
        out_jpg: 输出 jpg 路径。

    Returns:
        落盘后的 jpg 绝对路径。

    Raises:
        FFmpegError: 抽帧失败。
    """
    video_abs = os.path.abspath(video_path)
    out_abs = os.path.abspath(out_jpg)
    Path(out_abs).parent.mkdir(parents=True, exist_ok=True)
    exe = _require_ffmpeg()

    attempts = (
        ["-y", "-sseof", "-0.5", "-i", video_abs, "-update", "1", "-frames:v", "1", "-q:v", "3", out_abs],
        ["-y", "-i", video_abs, "-update", "1", "-frames:v", "1", "-q:v", "3", out_abs],
    )
    last_error = ""
    for args in attempts:
        try:
            proc = _run([exe] + args, timeout=90)
        except FFmpegError as exc:
            last_error = str(exc)
            if exc.code == ErrorCode.FFM_NOT_FOUND:
                raise
            continue
        if proc.returncode == 0 and os.path.exists(out_abs) and os.path.getsize(out_abs) > 0:
            return out_abs
        last_error = (proc.stderr or "")[-300:]

    raise FFmpegError(ErrorCode.FFM_EXEC_FAILED, f"抽取末帧失败：{last_error}")


# --------------------------------------------------------------------------- #
# 抽尾帧（PNG，供串行首帧链把上一段尾帧作为下一段首帧）
# --------------------------------------------------------------------------- #
def extract_tail_frame(video_path: str, out_png: str) -> str:
    """从视频抽取**最后一帧**输出为 PNG（供串行首帧链下一段复用）。

    Args:
        video_path: 源视频路径。
        out_png: 输出 png 路径。

    Returns:
        落盘后的 png 绝对路径。

    Raises:
        FFmpegError: 抽帧失败。
    """
    video_abs = os.path.abspath(video_path)
    out_abs = os.path.abspath(out_png)
    Path(out_abs).parent.mkdir(parents=True, exist_ok=True)
    exe = _require_ffmpeg()

    attempts = (
        ["-y", "-sseof", "-0.5", "-i", video_abs, "-frames:v", "1", out_abs],
        ["-y", "-i", video_abs, "-frames:v", "1", out_abs],
    )
    last_error = ""
    for args in attempts:
        try:
            proc = _run([exe] + args, timeout=90)
        except FFmpegError as exc:
            last_error = str(exc)
            if exc.code == ErrorCode.FFM_NOT_FOUND:
                raise
            continue
        if proc.returncode == 0 and os.path.exists(out_abs) and os.path.getsize(out_abs) > 0:
            _log.info("尾帧已抽取：%s", os.path.basename(out_abs))
            return out_abs
        last_error = (proc.stderr or "")[-300:]

    raise FFmpegError(ErrorCode.FFM_EXEC_FAILED, f"抽取尾帧失败：{last_error}")


# --------------------------------------------------------------------------- #
# 拼接
# --------------------------------------------------------------------------- #
def concat_videos(video_paths: list[str], out_mp4: str) -> str:
    """用 concat demuxer 按顺序拼接多个镜头为成片。

    先尝试无损 ``-c copy``；若失败则回退为重新编码（兼容异构输入）。

    Args:
        video_paths: 镜头视频路径列表（按顺序）。
        out_mp4: 输出成片路径。

    Returns:
        落盘后的 mp4 绝对路径。

    Raises:
        FFmpegError: 无输入或拼接失败。
    """
    paths = [os.path.abspath(p) for p in video_paths]
    if not paths:
        raise FFmpegError(ErrorCode.FFM_CONCAT_FAILED, "没有可拼接的镜头视频")

    out_abs = os.path.abspath(out_mp4)
    Path(out_abs).parent.mkdir(parents=True, exist_ok=True)
    exe = _require_ffmpeg()

    workdir = tempfile.mkdtemp(prefix="storyforge_concat_")
    try:
        list_file = Path(workdir) / "concat_list.txt"
        lines = []
        for path in paths:
            safe = path.replace("\\", "/").replace("'", "'\\''")
            lines.append(f"file '{safe}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        attempts = (
            [
                exe, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c", "copy", "-movflags", "+faststart", out_abs,
            ],
            [
                exe, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-an", "-movflags", "+faststart", out_abs,
            ],
        )

        last_error = ""
        for index, cmd in enumerate(attempts):
            try:
                proc = _run(cmd, timeout=600)
            except FFmpegError as exc:
                last_error = str(exc)
                if exc.code == ErrorCode.FFM_NOT_FOUND:
                    raise
                continue
            if proc.returncode == 0 and os.path.exists(out_abs) and os.path.getsize(out_abs) > 0:
                _log.info("视频拼接完成：%s（%d 个镜头）", os.path.basename(out_abs), len(paths))
                return out_abs
            last_error = (proc.stderr or "")[-400:]
            _log.warning("拼接方案 #%d 失败，尝试重新编码…", index + 1)

        raise FFmpegError(ErrorCode.FFM_CONCAT_FAILED, f"视频拼接失败：{last_error}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 剪辑：单片段裁剪 + 按 EDL 导出
# --------------------------------------------------------------------------- #
def trim_clip(
    src_mp4: str,
    out_mp4: str,
    start: float = 0.0,
    end: float | None = None,
    *,
    timeout: int = 300,
) -> str:
    """裁剪单个片段为独立 mp4（统一重编码，保证拼接编码一致）。

    采用「前置快速定位 + 后置精确时长」这一**无歧义**组合（不使用 ``-to``）：

    ``ffmpeg -y -ss {start} -i {src} -t {end - start} -c:v libx264 -preset veryfast
    -pix_fmt yuv420p -an -movflags +faststart {out}``

    - ``-ss`` 必须在 ``-i`` **之前**（输入定位，精度可达帧级），
    - ``-t`` 必须在 ``-i`` **之后**，且由程序算出 ``end - start``；
    - 全程经 :func:`_run` 以 list 形式调用，**不经 shell**；
    - ``start<=0 且 end is None`` → 整段直拷（``shutil.copy2``），避免无谓重编码。

    Args:
        src_mp4: 源片段路径。
        out_mp4: 输出片段路径。
        start: 裁剪起点（秒，>=0）。
        end: 裁剪终点（秒）；``None`` = 到片段结尾。
        timeout: 超时秒数。

    Returns:
        落盘后的输出片段绝对路径。

    Raises:
        FFmpegError: 源文件不存在、二进制缺失或执行失败。
    """
    src_abs = os.path.abspath(src_mp4)
    out_abs = os.path.abspath(out_mp4)
    Path(out_abs).parent.mkdir(parents=True, exist_ok=True)

    if not os.path.exists(src_abs):
        raise FFmpegError(ErrorCode.FFM_EXEC_FAILED, f"源视频不存在：{src_abs}")

    effective_start = max(0.0, float(start or 0.0))

    # 整段不裁剪：直接复制，避免重编码
    if effective_start <= 0.0 and end is None:
        shutil.copy2(src_abs, out_abs)
        return out_abs

    if end is None:
        end = probe_duration(src_abs)
    effective_end = float(end)
    if effective_end <= effective_start:
        # 无效区间（探测失败/终点早于起点）→ 回退整段复制，不丢片、不阻断
        shutil.copy2(src_abs, out_abs)
        return out_abs

    duration = effective_end - effective_start
    exe = _require_ffmpeg()
    cmd = [
        exe,
        "-y",
        "-ss",
        f"{effective_start:.3f}",
        "-i",
        src_abs,
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-an",
        "-movflags",
        "+faststart",
        out_abs,
    ]

    proc = _run(cmd, timeout=timeout)
    if proc.returncode != 0 or not os.path.exists(out_abs) or os.path.getsize(out_abs) == 0:
        raise FFmpegError(
            ErrorCode.FFM_EXEC_FAILED,
            f"裁剪片段失败：{(proc.stderr or '')[-400:]}",
        )
    _log.info("片段裁剪完成：%s（%.3f–%.3f，共 %.3fs）", os.path.basename(out_abs),
              effective_start, effective_end, duration)
    return out_abs


def export_from_edl(
    clips: "list[tuple[str, float, float | None]]",
    out_mp4: str,
    *,
    timeout: int = 600,
) -> str:
    """按 EDL 逐个裁剪再拼接为成片（统一编码，消除拼接异构）。

    所有裁剪片段统一用同一组编码参数（``libx264 / veryfast / yuv420p``）重编码，
    从根本上规避 concat 输入编码/时基/像素格式不一致导致的失败或花屏；
    ``concat_videos`` 内置的「``-c copy`` 失败 → 重编码」作为第二道保险。
    裁剪失败时该片段降级为「整段复制」。

    Args:
        clips: ``[(src_path, start, end), …]``，``end`` 为 ``None`` 表示到片段结尾。
        out_mp4: 输出成片路径。
        timeout: 每次 ffmpeg 调用超时秒数。

    Returns:
        落盘后的 mp4 绝对路径。

    Raises:
        FFmpegError: 无输入（``FFM_CONCAT_FAILED``）或拼接失败。
    """
    if not clips:
        raise FFmpegError(ErrorCode.FFM_CONCAT_FAILED, "没有可导出的片段")

    out_abs = os.path.abspath(out_mp4)
    Path(out_abs).parent.mkdir(parents=True, exist_ok=True)

    workdir = tempfile.mkdtemp(prefix="storyforge_export_")
    try:
        trimmed: list[str] = []
        for index, item in enumerate(clips):
            src = item[0]
            start = item[1] if len(item) > 1 else 0.0
            end = item[2] if len(item) > 2 else None
            clip_path = os.path.join(workdir, f"clip_{index:03d}.mp4")
            try:
                trimmed.append(trim_clip(src, clip_path, start, end, timeout=timeout))
            except FFmpegError as exc:
                if exc.code == ErrorCode.FFM_NOT_FOUND:
                    # 二进制缺失：降级无意义，直接上抛
                    raise
                _log.warning("片段 %d 裁剪失败，降级为整段复制：%s", index, exc)
                shutil.copy2(os.path.abspath(src), clip_path)
                trimmed.append(clip_path)

        return concat_videos(trimmed, out_abs)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 时长探测
# --------------------------------------------------------------------------- #
def probe_duration(path: str) -> float:
    """探测媒体文件时长（秒）。

    优先使用 ffprobe；缺失时回退解析 ``ffmpeg -i`` 的 stderr。

    Args:
        path: 媒体文件路径。

    Returns:
        时长（秒）；无法探测时返回 ``0.0``。
    """
    target = os.path.abspath(path)
    if not os.path.exists(target):
        return 0.0

    ffprobe = _ffprobe_bin()
    if ffprobe:
        try:
            proc = _run(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1",
                    target,
                ],
                timeout=60,
            )
            if proc.returncode == 0:
                value = (proc.stdout or "").strip()
                if value:
                    return float(value)
        except (FFmpegError, ValueError):
            pass

    # 回退：解析 ffmpeg -i 的输出
    try:
        proc = _run([_ffmpeg_bin(), "-i", target], timeout=60)
    except FFmpegError:
        return 0.0
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
    if match:
        hours, minutes, seconds = int(match.group(1)), int(match.group(2)), float(match.group(3))
        return hours * 3600 + minutes * 60 + seconds
    return 0.0
