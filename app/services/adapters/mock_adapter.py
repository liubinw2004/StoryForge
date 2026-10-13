"""Mock 适配器（P0）：调用 FFmpeg 生成渐变 + 文字 + 编号 + 进度条的占位视频。

零成本、无需网络与 Key，保证全流程可跑通。输出 1280x720（默认）/24fps/mp4(H.264)。
"""

from __future__ import annotations

from app.models.enums import VideoProvider
from app.services.adapters.base import VideoError, VideoGeneratorInterface
from app.utils.ffmpeg_utils import FFmpegError, make_mock_video, probe_ffmpeg
from app.utils.responses import ErrorCode

__all__ = ["MockAdapter"]


class MockAdapter(VideoGeneratorInterface):
    """Mock 视频适配器：用 FFmpeg lavfi 生成占位视频。"""

    name = VideoProvider.MOCK

    # 渐变配色循环（按镜头切换，画面更易区分）
    _PALETTE: tuple[tuple[str, str], ...] = (
        ("0x2563EB", "0xA855F7"),  # 亮蓝 → 亮紫
        ("0x0D9488", "0x38BDF8"),  # 青绿 → 天蓝
        ("0xF59E0B", "0xEF4444"),  # 琥珀 → 红
        ("0x7C3AED", "0xEC4899"),  # 紫罗兰 → 品红
        ("0x059669", "0x84CC16"),  # 翠绿 → 黄绿
        ("0x334155", "0x64748B"),  # 石板灰 → 浅灰
    )

    def generate(
        self,
        prompt: str,
        image_ref: str | None,
        duration: int,
        size: str,
        style: str,
        out_path: str,
    ) -> str:
        """生成占位视频。

        ``prompt`` 约定：首行为镜头编号（如 ``SHOT 3 / 6``），其余为正文
        （场景 / 对白 / 画面）。``image_ref`` 在本适配器中被忽略（视觉连续性
        仍由上层抽取末帧并落盘以保证链路打通）。
        """
        width, height = self._validate_params(duration, size, out_path)

        lines = [line.strip() for line in (prompt or "").splitlines() if line.strip()]
        header = lines[0] if lines else "SHOT"
        body = lines[1:] if len(lines) > 1 else []

        index = (abs(hash(header)) % len(self._PALETTE))
        color0, color1 = self._PALETTE[index]

        try:
            return make_mock_video(
                out_path,
                header=header,
                body_lines=body,
                duration=int(duration),
                width=width,
                height=height,
                fps=24,
                color0=color0,
                color1=color1,
            )
        except FFmpegError as exc:
            raise VideoError(exc.code, exc.message) from exc
        except Exception as exc:
            raise VideoError(ErrorCode.VID_GENERATE_FAILED, f"Mock 视频生成失败：{exc}") from exc

    def test_connection(self) -> dict:
        """Mock 模式只要 FFmpeg 可用即视为就绪。"""
        info = probe_ffmpeg()
        if info["available"]:
            return {
                "ok": True,
                "provider": self.name.value,
                "message": f"Mock 模式就绪（FFmpeg {info['version'] or '可用'}）",
            }
        return {"ok": False, "provider": self.name.value, "message": info["message"]}

    def generate_i2v(
        self,
        first_frame: str,
        prompt: str,
        duration: int,
        seed: int,
        size: str,
        out_path: str,
    ) -> str:
        """生成占位视频（工作流 i2v 的零成本本地替身）。

        ``first_frame`` / ``seed`` 在本适配器中被**忽略**（纯本地占位，不访问网络、
        不读首帧文件），仅用于保持与 :class:`RunningHubAdapter` 同构的接口。视觉连续性
        仍由上层抽取尾帧并落盘以保证链路打通。

        Args:
            first_frame: 首帧图路径（忽略）。
            prompt: 提示词（首行作为镜头编号，其余作为正文）。
            duration: 时长（秒）。
            seed: 随机种子（忽略）。
            size: 尺寸字符串，如 ``1280x720``。
            out_path: 输出 mp4 绝对路径。

        Returns:
            落盘后的绝对路径。

        Raises:
            VideoError: FFmpeg 不可用或生成失败。
        """
        width, height = self._validate_params(duration, size, out_path)

        lines = [line.strip() for line in (prompt or "").splitlines() if line.strip()]
        header = lines[0] if lines else "SEGMENT"
        body = lines[1:] if len(lines) > 1 else []

        index = (abs(hash(header)) % len(self._PALETTE))
        color0, color1 = self._PALETTE[index]

        try:
            return make_mock_video(
                out_path,
                header=header,
                body_lines=body,
                duration=int(duration),
                width=width,
                height=height,
                fps=24,
                color0=color0,
                color1=color1,
            )
        except FFmpegError as exc:
            raise VideoError(exc.code, exc.message) from exc
        except Exception as exc:
            raise VideoError(ErrorCode.VID_GENERATE_FAILED, f"Mock 视频生成失败：{exc}") from exc
