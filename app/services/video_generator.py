"""视频生成服务：业务层唯一入口，按配置路由到具体适配器。"""

from __future__ import annotations

from app.models.enums import VideoProvider
from app.models.schemas import Config
from app.services.adapters import ADAPTER_REGISTRY, VideoError, VideoGeneratorInterface
from app.utils.logger import get_logger
from app.utils.responses import ErrorCode

__all__ = ["VideoGenerator", "VideoError"]

_log = get_logger("video")


class VideoGenerator:
    """视频生成统一入口。

    上层（DirectorAgent）只依赖本类，不感知底层适配器差异。
    """

    def __init__(self, cfg: Config) -> None:
        """初始化。

        Args:
            cfg: 全局配置，用于决定使用哪个适配器。
        """
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    def _resolve_adapter(self) -> VideoGeneratorInterface:
        """按 ``cfg.video_provider`` 解析出适配器实例。

        Returns:
            适配器实例。

        Raises:
            VideoError: 未注册对应提供方。
        """
        provider: VideoProvider = self.cfg.video_provider
        adapter_cls = ADAPTER_REGISTRY.get(provider)
        if adapter_cls is None:
            raise VideoError(
                ErrorCode.VID_ADAPTER_MISSING,
                f"未找到视频提供方 {provider} 对应的适配器，请检查配置",
            )
        return adapter_cls(self.cfg)

    def generate(
        self,
        prompt: str,
        image_ref: str | None,
        duration: int,
        size: str,
        style: str,
        out_path: str,
    ) -> str:
        """生成单个镜头视频。

        Args:
            prompt: 提示词。
            image_ref: 参考图路径（视觉连续性）。
            duration: 时长（秒）。
            size: 尺寸字符串。
            style: 风格。
            out_path: 输出 mp4 绝对路径。

        Returns:
            落盘后的视频绝对路径。
        """
        adapter = self._resolve_adapter()
        _log.info(
            "使用适配器 %s 生成镜头：duration=%ss size=%s style=%s",
            adapter.name.value,
            duration,
            size,
            style,
        )
        return adapter.generate(prompt, image_ref, duration, size, style, out_path)

    def test_connection(self) -> dict:
        """测试当前配置对应适配器的可用性。

        Returns:
            ``{"ok": bool, "provider": str, "message": str}``
        """
        try:
            adapter = self._resolve_adapter()
            return adapter.test_connection()
        except VideoError as exc:
            return {
                "ok": False,
                "provider": self.cfg.video_provider.value,
                "message": str(exc),
            }
        except Exception as exc:  # 兜底，不向调用方抛异常
            return {
                "ok": False,
                "provider": self.cfg.video_provider.value,
                "message": f"{ErrorCode.VID_ADAPTER_MISSING}: {exc}",
            }

    # ------------------------------------------------------------------ #
    # 工作流 i2v（图生视频，串行首帧链）
    # ------------------------------------------------------------------ #
    def generate_i2v(
        self,
        first_frame: str,
        prompt: str,
        duration: int,
        seed: int,
        size: str,
        out_path: str,
    ) -> str:
        """走工作流 i2v 生成单段视频（上传首帧 → 提交 → 轮询 → 下载）。

        Args:
            first_frame: 首帧图本地路径（必填）。
            prompt: 提示词。
            duration: 时长（秒）。
            seed: 随机种子。
            size: 尺寸字符串。
            out_path: 输出 mp4 绝对路径。

        Returns:
            落盘后的视频绝对路径。

        Raises:
            VideoError: 适配器生成失败。
        """
        adapter = self._resolve_adapter()
        _log.info(
            "使用适配器 %s 工作流 i2v：duration=%ss seed=%s",
            adapter.name.value,
            duration,
            seed,
        )
        return adapter.generate_i2v(first_frame, prompt, duration, seed, size, out_path)
