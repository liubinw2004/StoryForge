"""视频适配器注册表：provider → 适配器类（工作流 i2v 单一视频路径）。"""

from __future__ import annotations

from app.models.enums import VideoProvider
from app.services.adapters.base import VideoError, VideoGeneratorInterface
from app.services.adapters.mock_adapter import MockAdapter
from app.services.adapters.runninghub_adapter import RunningHubAdapter

__all__ = [
    "VideoError",
    "VideoGeneratorInterface",
    "MockAdapter",
    "RunningHubAdapter",
    "ADAPTER_REGISTRY",
]

#: 视频适配器注册表：按 :class:`VideoProvider` 路由到具体实现类。
ADAPTER_REGISTRY: dict[VideoProvider, type[VideoGeneratorInterface]] = {
    VideoProvider.MOCK: MockAdapter,
    VideoProvider.RUNNINGHUB: RunningHubAdapter,
}
