"""DirectorAgent：为串行首帧链构建**工作流提示词**（瘦身版）。

要点：

- 首帧降级阶梯（L1 定妆照 → L2 场景锚图 → L3 上一镜末帧 → L4 纯文生）已**整体移除**：
  视频生成改走 RunningHub 工作流 i2v 的**串行首帧链**（由 ``FirstFrameChain`` 编排），
  导演层只保留「构造单段提示词」这一纯函数职责；
- ``build_prompt``：Mock 模式返回中文占位正文（首行 ``SEGMENT i / total``），
  真实模式走 :func:`build_video_prompt` 拼装「风格前缀 + 场景锁定卡 + 角色锁定卡 + 镜头主体」，
  外观一致性由提示词锁定卡（``consistency_lock``）承载。
"""

from __future__ import annotations

from pathlib import Path

from app.agents.base_agent import BaseAgent
from app.models.enums import VideoProvider
from app.models.schemas import Config, ScriptDocument, Shot
from app.services.prompt_templates import build_video_prompt

__all__ = ["DirectorAgent"]


class DirectorAgent(BaseAgent):
    """脚本 → 单段工作流提示词（串行首帧链的提示词构造器）。"""

    def __init__(self, llm, video_generator=None) -> None:
        """初始化。

        Args:
            llm: LLM 客户端（保留，供未来镜头增强使用；``build_prompt`` 不依赖它）。
            video_generator: 视频生成统一入口（保留，供未来扩展；当前不使用）。
        """
        super().__init__(llm, name="director_agent")
        self.video = video_generator

    # ------------------------------------------------------------------ #
    def build_prompt(
        self,
        shot: Shot,
        script: ScriptDocument,
        index: int,
        total: int,
        config: Config,
    ) -> str:
        """构造传给视频适配器的单段提示词。

        - Mock 模式：首行 ``SEGMENT i / total``，其余为可显示的中文正文；
        - 真实模式（``consistency_lock`` 为真）：用 :func:`build_video_prompt` 拼装
          「风格前缀 + 场景锁定卡 + 角色锁定卡 + 镜头主体」，锁定外观一致性；
        - 真实模式（``consistency_lock`` 为假）：保留旧逻辑（英文 prompt）。

        Args:
            shot: 镜头定义。
            script: 所属脚本（提供场景/角色锁定卡）。
            index: 镜头序号（从 1 开始）。
            total: 镜头总数。
            config: 全局配置。

        Returns:
            单段提示词字符串。
        """
        if config.video_provider == VideoProvider.MOCK:
            return "\n".join(
                [
                    f"SEGMENT {index} / {total}",
                    f"场景：{script.scene or '—'}",
                    f"对白：{shot.dialogue or '—'}",
                    f"画面：{shot.action or shot.prompt or '—'}",
                ]
            )

        if getattr(config, "consistency_lock", True):
            scene_card = self._scene_card(script, getattr(shot, "scene_index", 0))
            return build_video_prompt(
                shot.prompt or shot.action,
                config.style,
                [(c.name, c.lock_card) for c in script.characters if c.lock_card],
                scene_card,
            )

        return shot.prompt or shot.action or ""

    @staticmethod
    def _scene_card(script: ScriptDocument, scene_index: int) -> str:
        """取某场景的锁定卡；越界或缺失时回落到 ``script.scene``。"""
        scenes = list(getattr(script, "scenes", []) or [])
        if 0 <= scene_index < len(scenes):
            card = str(getattr(scenes[scene_index], "lock_card", "") or "").strip()
            if card:
                return card
        return str(getattr(script, "scene", "") or "").strip()

    @staticmethod
    def _to_task_relative(path: str, output_dir: Path) -> str:
        """把绝对路径转换为相对任务目录的正斜杠路径（供前端拼接 URL）。"""
        try:
            return Path(path).resolve().relative_to(output_dir.resolve()).as_posix()
        except ValueError:
            return Path(path).name
