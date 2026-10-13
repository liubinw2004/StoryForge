"""视频生成适配器抽象基类。

所有适配器对外输出必须统一为 ``mp4 / H.264``，确保下游 FFmpeg 拼接环节
对 Mock 与真实适配器**完全无差异化代码**（见 ARCHITECTURE §8 R9）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.enums import VideoProvider
from app.models.schemas import Config
from app.utils.responses import ErrorCode, err_msg

__all__ = ["VideoError", "VideoGeneratorInterface"]


class VideoError(Exception):
    """视频生成异常，携带 ``VID_*`` 错误码。"""

    def __init__(self, code: str, message: str) -> None:
        """初始化异常。

        Args:
            code: 错误码（:class:`app.utils.responses.ErrorCode`）。
            message: 中文可读信息。
        """
        self.code = code
        self.message = message
        super().__init__(err_msg(code, message))


class VideoGeneratorInterface(ABC):
    """视频生成适配器统一接口。"""

    name: VideoProvider

    def __init__(self, cfg: Config) -> None:
        """初始化。

        Args:
            cfg: 全局配置（提供 api_key / base_url / 分辨率等）。
        """
        self.cfg = cfg

    @abstractmethod
    def generate(
        self,
        prompt: str,
        image_ref: str | None,
        duration: int,
        size: str,
        style: str,
        out_path: str,
    ) -> str:
        """生成单个镜头，返回落盘后的视频绝对路径。

        Args:
            prompt: 提示词（Mock 适配器将其首行作为镜头编号、其余作为正文）。
            image_ref: 参考图路径（视觉连续性），Mock 可忽略。
            duration: 时长（秒）。
            size: 尺寸字符串，如 ``1280x720``。
            style: 风格（realistic/anime/animation）。
            out_path: 输出 mp4 绝对路径。

        Returns:
            落盘后的绝对路径。

        Raises:
            VideoError: 参数非法或生成失败。
        """

    @abstractmethod
    def test_connection(self) -> dict:
        """测试适配器可用性。

        Returns:
            ``{"ok": bool, "provider": str, "message": str}``
        """

    # ------------------------------------------------------------------ #
    # 公共参数校验工具
    # ------------------------------------------------------------------ #
    def _validate_params(self, duration: int, size: str, out_path: str) -> tuple[int, int]:
        """校验并解析通用参数。

        Args:
            duration: 时长（秒）。
            size: 尺寸字符串。
            out_path: 输出路径。

        Returns:
            ``(width, height)``

        Raises:
            VideoError: 参数非法。
        """
        if not out_path:
            raise VideoError(ErrorCode.VID_INVALID_PARAM, "输出路径不能为空")
        if duration is None or int(duration) <= 0:
            raise VideoError(ErrorCode.VID_INVALID_PARAM, f"时长非法：{duration}（应为正整数秒）")
        width, height = self._parse_size(size)
        return width, height

    def _parse_size(self, size: str | None) -> tuple[int, int]:
        """解析尺寸字符串，非法时回退到配置分辨率。"""
        if size:
            match = None
            import re

            match = re.match(r"^\s*(\d+)\s*[xX*]\s*(\d+)\s*$", str(size))
            if match:
                return int(match.group(1)), int(match.group(2))
        return self.cfg.resolution.size
