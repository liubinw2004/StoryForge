"""Pydantic 数据模型：配置、脚本契约、任务状态、评估报告、请求体。

约定：
- 所有模型 ``model_config = ConfigDict(extra="ignore")``，容忍 LLM 多返回的字段；
- 列表字段一律使用 ``Field(default_factory=list)``，避免可变默认值陷阱；
- 字段名与 ``docs/ARCHITECTURE.md`` §3.2、API 响应对齐。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    LLMMode,
    Resolution,
    ShotState,
    TaskState,
    VideoProvider,
)

__all__ = [
    "mask_secret",
    "Config",
    "Character",
    "Scene",
    "Shot",
    "ScriptDocument",
    "StoryBeat",
    "StoryDocument",
    "StageStatus",
    "ShotResult",
    "TaskStatus",
    "ScriptScore",
    "VideoScore",
    "EvaluationReport",
    "PipelineRunRequest",
    "ConfirmScriptRequest",
    "RetryShotRequest",
    "ClipEdit",
    "ExportRequest",
]

_BASE_CONFIG = ConfigDict(extra="ignore")


def mask_secret(value: str) -> str:
    """将敏感串脱敏为 ``sk-****abcd`` 形式。

    Args:
        value: 原始密钥串（可为空）。

    Returns:
        脱敏后的字符串；空串返回空串，过短（≤8）返回 ``****``。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:3]}****{value[-4:]}"


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #
class Config(BaseModel):
    """用户配置（明文持久化到 ``config/config.json``，对外返回脱敏副本）。"""

    model_config = _BASE_CONFIG

    llm_mode: LLMMode = LLMMode.OLLAMA
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = ""
    llm_model: str = "qwen2.5:14b"

    video_provider: VideoProvider = VideoProvider.MOCK
    video_api_key: str = ""
    video_base_url: str = ""
    video_model: str = ""

    resolution: Resolution = Resolution.P720
    style: str = "realistic"
    shot_count: int = Field(default=4, ge=2, le=8)
    shot_duration: int = Field(default=4, ge=2, le=10)

    skip_video_eval: bool = False

    # ---- 人物/场景一致性增量（提示词锁定卡，保留）----
    consistency_lock: bool = True     # 提示词锁定卡（角色/场景外观锁定）

    # ---- RunningHub 工作流 i2v 增量（全部带默认值 → 旧 JSON 兼容）----
    # API Key 复用既有 video_api_key（已在 _SECRET_FIELDS 遮罩），不新增 rh_api_key。
    rh_workflow_id: str = "2084067455902765057"   # 提交端点 /openapi/v2/run/workflow/{id}
    rh_instance_type: str = "default"             # default(24G)/plus(48G)/ultra(84G)
    rh_retain_seconds: int = 180                  # 结果保留秒数（10–180，加载时钳制）
    rh_first_frame_node_id: str = "4"             # 首帧图节点
    rh_first_frame_field_name: str = "image"
    rh_prompt_node_id: str = "7"                  # 提示词节点
    rh_prompt_field_name: str = "prompt"
    rh_duration_node_id: str = "6"                # 时长节点
    rh_duration_field_name: str = "duration_seconds"
    rh_seed_node_id: str = "9"                    # 种子节点
    rh_seed_field_name: str = "seed"

    def to_public(self) -> "Config":
        """返回 api_key 脱敏后的副本（不修改原始对象）。"""
        clone = self.model_copy(deep=True)
        clone.llm_api_key = mask_secret(self.llm_api_key)
        clone.video_api_key = mask_secret(self.video_api_key)
        return clone


# --------------------------------------------------------------------------- #
# 脚本契约（对齐 PRD §3.7）
# --------------------------------------------------------------------------- #
class Character(BaseModel):
    """角色定义。"""

    model_config = _BASE_CONFIG

    name: str = ""
    desc: str = ""
    lock_card: str = ""   # 英文外观锁定卡（发型/脸型/年龄/服饰/体型等不随时间变化的特征）


class Scene(BaseModel):
    """场景定义（增量：人物/场景一致性——同场景镜头共享一张场景锁定卡）。

    与 ``ScriptDocument.scene``（旧的单场景总述）并存：``scenes`` 是按剧情切分的
    有序场景列表，``shot.scene_index`` 指向其中之一；``scene`` 保留为整片场景总述。
    """

    model_config = _BASE_CONFIG

    index: int = 0        # 场景序号（从 0 起，同场景 shot 连续）
    name: str = ""        # 场景名（如「咖啡馆-白天」）
    desc: str = ""        # 场景中文描述
    lock_card: str = ""   # 英文场景锁定卡（地点/时段/光线/色调/关键陈设）


class Shot(BaseModel):
    """单个镜头。"""

    model_config = _BASE_CONFIG

    shot_id: str = ""            # 如 "shot_001"
    dialogue: str = ""           # 对白
    action: str = ""             # 动作/画面
    camera: str = ""             # 运镜（如"中景，缓慢推进"）
    duration: int = 4            # 秒
    prompt: str = ""             # 面向视频模型的英文提示词
    scene_index: int = 0         # 所属场景序号（增量：同场景连续、从 0 起）


class ScriptDocument(BaseModel):
    """ScriptAgent 输出 / DirectorAgent 输入的统一脚本契约。"""

    model_config = _BASE_CONFIG

    title: str = "未命名短剧"
    style: str = "realistic"
    characters: list[Character] = Field(default_factory=list)
    scene: str = ""
    positions: list[str] = Field(default_factory=list)
    shots: list[Shot] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)   # 增量：有序场景列表


# --------------------------------------------------------------------------- #
# 剧情契约（增量：剧情补全）
# --------------------------------------------------------------------------- #
class StoryBeat(BaseModel):
    """故事情节节点（起承转合中的一个节点）。"""

    model_config = _BASE_CONFIG

    index: int = 0           # 节点序号（1 起；规范化时按数组顺序重排为 1..N）
    summary: str = ""        # 该节点发生了什么
    purpose: str = ""        # 叙事作用（铺垫/冲突/转折/收尾）
    location: str = ""       # 该节点发生地点


class StoryDocument(BaseModel):
    """StoryAgent 输出：由粗糙对话补全得到的完整故事情节。

    作为 ScriptAgent 的输入来源（``synopsis + beats + characters + setting``），
    承载「完整故事情节」，其 ``shots[].prompt`` 即所需的分镜提示词。
    """

    model_config = _BASE_CONFIG

    title: str = "未命名故事"
    logline: str = ""                              # 一句话梗概（谁 + 想要什么 + 阻碍）
    synopsis: str = ""                             # 完整故事情节（多段正文）
    setting: str = ""                              # 时空/场景总设定（供 ScriptDocument.scene 复用）
    characters: list[Character] = Field(default_factory=list)
    beats: list[StoryBeat] = Field(default_factory=list)
    genre: str = ""                                # 题材/类型（可由 style 推导）
    theme: str = ""                                # 主题立意
    source_dialogue: str = ""                      # 原始粗糙对话（溯源）
    extra: dict = Field(default_factory=dict)      # 预留扩展位


# --------------------------------------------------------------------------- #
# 任务与镜头状态
# --------------------------------------------------------------------------- #
class StageStatus(BaseModel):
    """流水线单阶段状态。"""

    model_config = _BASE_CONFIG

    name: str = ""                                     # script/director/concat/critic
    state: TaskState = TaskState.PENDING
    detail: str = ""                                   # 如 "shot_003/006"
    elapsed: float = 0.0                               # 本阶段耗时（秒）


class ShotResult(BaseModel):
    """单镜头生成结果。"""

    model_config = _BASE_CONFIG

    shot_id: str = ""
    state: ShotState = ShotState.WAITING
    video_path: str | None = None                      # 相对任务目录，如 "shots/shot_001.mp4"
    frame_path: str | None = None                      # 相对任务目录，如 "frames/frame_001.jpg"
    first_frame_path: str | None = None                # 增量：逐镜首帧（任务相对正斜杠路径）
    error: str | None = None
    cost_seconds: float = 0.0
    duration: float = 0.0                              # 片段真实时长（秒，probe 失败为 0.0）


class TaskStatus(BaseModel):
    """任务整体状态（前端轮询对象）。"""

    model_config = _BASE_CONFIG

    task_id: str = ""
    state: TaskState = TaskState.PENDING
    output_dir: str = ""                               # 相对路径 "outputs/<ts>"
    stages: dict[str, StageStatus] = Field(default_factory=dict)
    shots: list[ShotResult] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0


# --------------------------------------------------------------------------- #
# 评估报告（脚本 4 维 + 视频 3 维，1–10 分）
# --------------------------------------------------------------------------- #
class ScriptScore(BaseModel):
    """脚本质量评分（4 维度）。"""

    model_config = _BASE_CONFIG

    coherence: float = 0.0              # 连贯性
    shootability: float = 0.0           # 可拍性
    character_consistency: float = 0.0  # 角色一致性
    plot_completeness: float = 0.0      # 剧情完整度
    total: float = 0.0                  # 四维均值
    comments: dict[str, str] = Field(default_factory=dict)


class VideoScore(BaseModel):
    """视频保真度评分（3 维度）。"""

    model_config = _BASE_CONFIG

    visual_consistency: float = 0.0     # 画面一致性
    dialogue_match: float = 0.0         # 对白匹配
    style_fidelity: float = 0.0         # 风格符合度
    total: float = 0.0
    comments: dict[str, str] = Field(default_factory=dict)


class EvaluationReport(BaseModel):
    """CriticAgent 输出：完整评估报告。"""

    model_config = _BASE_CONFIG

    task_id: str = ""
    script: ScriptScore = Field(default_factory=ScriptScore)
    video: VideoScore | None = None
    video_skipped: bool = False
    comment: str = ""


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class PipelineRunRequest(BaseModel):
    """启动流水线请求体。"""

    model_config = _BASE_CONFIG

    dialogue: str = Field(min_length=1)
    style: str = "realistic"
    shot_count: int = Field(default=4, ge=2, le=8)
    shot_duration: int = Field(default=4, ge=2, le=10)
    resolution: Resolution = Resolution.P720
    skip_video_eval: bool = False
    story: StoryDocument | None = None      # 外部提供的完整剧情（提供时跳过 StoryAgent 调用）
    skip_story: bool = False                # 跳过剧情补全，直接用 dialogue 走 str 兜底路径
    review: bool = False                    # 增量：True 时 script 阶段后挂起等待人工确认（默认 False=旧路径）


class ConfirmScriptRequest(BaseModel):
    """分镜确认请求体（``POST /api/pipeline/{task_id}/confirm-script``）。

    - ``story``：携带则视为「完整剧情可能已变更」；与记录不一致时重跑 ``ScriptAgent``；
    - ``script``：仅当 ``story`` 未变更（未提供或与记录相等）时采用；
    - 二者均可选、可同时携带；均缺省则用现有脚本直接续跑。
    """

    model_config = _BASE_CONFIG

    story: StoryDocument | None = None      # 可编辑后的完整剧情（变更即重跑脚本）
    script: ScriptDocument | None = None    # 人工编辑后的分镜脚本（story 未变时采用）


class RetryShotRequest(BaseModel):
    """重试单个镜头请求体。"""

    model_config = _BASE_CONFIG

    shot_id: str = Field(min_length=1)


# --------------------------------------------------------------------------- #
# 剪辑请求模型（增量：视频剪辑区）
# --------------------------------------------------------------------------- #
class ClipEdit(BaseModel):
    """单片段剪辑指令（EDL 元素）。"""

    model_config = _BASE_CONFIG

    shot_id: str = ""            # 片段标识（对应 ShotResult.shot_id）
    clip_id: str | None = None   # 增量（可选）：有效 id = clip_id or shot_id；split 后各段唯一
    enabled: bool = True         # 是否参与预览与导出
    start: float = 0.0           # 裁剪起点（秒，>=0）
    end: float | None = None     # 裁剪终点（秒）；None = 到片段结尾
    order: int | None = None     # 可选显式顺序；缺省以 clips 数组顺序为准


class ExportRequest(BaseModel):
    """导出剪辑方案请求体。"""

    model_config = _BASE_CONFIG

    clips: list[ClipEdit] = Field(default_factory=list)   # 有序剪辑方案
