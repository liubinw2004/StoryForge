"""数据模型层：Pydantic 模型与枚举。

统一从本包导出，便于其他模块 ``from app.models import Config`` 之类的写法。
"""

from __future__ import annotations

from app.models.enums import (
    LLMMode,
    Resolution,
    ShotState,
    TaskState,
    VideoProvider,
)
from app.models.schemas import (
    Character,
    Config,
    EvaluationReport,
    PipelineRunRequest,
    RetryShotRequest,
    ScriptDocument,
    ScriptScore,
    Shot,
    ShotResult,
    StageStatus,
    TaskStatus,
    VideoScore,
    mask_secret,
)

__all__ = [
    # 枚举
    "LLMMode",
    "VideoProvider",
    "Resolution",
    "TaskState",
    "ShotState",
    # 配置
    "Config",
    "mask_secret",
    # 脚本契约
    "Character",
    "Shot",
    "ScriptDocument",
    # 任务与镜头
    "StageStatus",
    "ShotResult",
    "TaskStatus",
    # 评估报告
    "ScriptScore",
    "VideoScore",
    "EvaluationReport",
    # 请求模型
    "PipelineRunRequest",
    "RetryShotRequest",
]
