"""枚举定义：LLM 模式、视频提供方、分辨率、任务/镜头状态。

所有枚举均继承 ``str, Enum``，可直接序列化为字符串，便于 API 传输与前端渲染。
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "LLMMode",
    "VideoProvider",
    "Resolution",
    "TaskState",
    "ShotState",
]


class LLMMode(str, Enum):
    """LLM 接入模式。"""

    OPENAI = "openai"      # 模式A：OpenAI 兼容接口（DeepSeek / 通义 / GLM / Kimi / OpenAI …）
    OLLAMA = "ollama"      # 模式B：本地 Ollama（无需 api_key）

    @property
    def label(self) -> str:
        """返回中文显示名。"""
        return {"openai": "OpenAI 兼容接口", "ollama": "本地 Ollama"}[self.value]


class VideoProvider(str, Enum):
    """视频生成提供方（适配器路由键）。

    - ``MOCK``：P0 必装，FFmpeg 占位视频，零成本；
    - ``RUNNINGHUB``：真实 API，RunningHub **工作流 i2v**（图生视频，按次计费）。
    """

    MOCK = "mock"                # P0 必装：FFmpeg 占位视频，零成本
    RUNNINGHUB = "runninghub"    # 真实 API：RunningHub 工作流 i2v（图生视频，按次计费）

    @property
    def label(self) -> str:
        """返回中文显示名。"""
        return {
            "mock": "模拟模式（Mock，零成本）",
            "runninghub": "RunningHub（工作流 i2v）",
        }[self.value]


class Resolution(str, Enum):
    """视频分辨率（Mock 与真实适配器共用）。"""

    P720 = "720p"    # 1280x720
    P1080 = "1080p"  # 1920x1080

    @property
    def size(self) -> tuple[int, int]:
        """返回 (宽, 高) 像素元组。"""
        return (1280, 720) if self is Resolution.P720 else (1920, 1080)

    @property
    def label(self) -> str:
        """返回形如 ``1280x720`` 的尺寸字符串。"""
        width, height = self.size
        return f"{width}x{height}"


class TaskState(str, Enum):
    """任务级状态机：pending → running → success|failed（增量新增挂起态）。

    - ``pending → running → success|failed``：旧路径（``review=false``）逐字不变；
    - ``running → awaiting_review → running``：人审确认路径（**非终态**，
      ``is_terminal`` 仍只认 ``SUCCESS/FAILED``，故 ``set_state`` 不写 ``finished_at``，
      前端持续轮询）。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    AWAITING_REVIEW = "awaiting_review"  # 增量：script 完成、等待人工确认分镜（非终态）

    @property
    def is_terminal(self) -> bool:
        """是否为终态（终态应停止前端轮询）。

        ``AWAITING_REVIEW`` **刻意不是终态**：前端须继续 1s 轮询，等待
        ``POST /confirm-script`` 触发原地续跑。
        """
        return self in (TaskState.SUCCESS, TaskState.FAILED)


class ShotState(str, Enum):
    """镜头级状态机：waiting → running → success|failed。"""

    WAITING = "waiting"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
