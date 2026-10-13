"""CriticAgent：对脚本质量与视频保真度进行 LLM 打分（1–10）。

- 脚本评估：4 维度（连贯性 / 可拍性 / 角色一致性 / 剧情完整度）；
- 视频评估：3 维度（画面一致性 / 对白匹配 / 风格符合度）；
- ``skip_video_eval=True`` 时仅评估脚本，报告标记 ``video_skipped=True``。
"""

from __future__ import annotations

from typing import Any

from app.agents.base_agent import BaseAgent
from app.models.schemas import (
    EvaluationReport,
    ScriptDocument,
    ScriptScore,
    ShotResult,
    VideoScore,
)
from app.services.prompt_templates import (
    critic_script_system_prompt,
    critic_script_user_prompt,
    critic_video_system_prompt,
    critic_video_user_prompt,
    shots_summary,
)

__all__ = ["CriticAgent"]


class CriticAgent(BaseAgent):
    """质量评估智能体。"""

    def __init__(self, llm) -> None:
        """初始化。

        Args:
            llm: LLM 客户端。
        """
        super().__init__(llm, name="critic_agent")

    def run(
        self,
        script: ScriptDocument,
        shots: list[ShotResult],
        skip_video: bool = False,
        *,
        task_id: str = "",
    ) -> EvaluationReport:
        """生成评估报告。

        Args:
            script: 分镜脚本。
            shots: 镜头结果列表。
            skip_video: 是否跳过视频评估。
            task_id: 任务 ID（写入报告）。

        Returns:
            :class:`EvaluationReport`。
        """
        script_json = script.model_dump_json(indent=2, ensure_ascii=False)

        script_score = self._evaluate_script(script_json)

        video_score: VideoScore | None = None
        video_skipped = False
        comment = script_score.comments.get("comment", "") or script_score.comments.get("total", "")

        if skip_video:
            video_skipped = True
            self.log.info("已按配置跳过视频评估")
        else:
            try:
                video_score = self._evaluate_video(script_json, shots)
                if video_score.comments.get("comment"):
                    comment = video_score.comments["comment"]
            except Exception as exc:  # 视频评估失败降级为「已跳过」，不影响整体报告
                video_skipped = True
                self.log.warning("视频评估失败，已降级为跳过：%s", exc)

        return EvaluationReport(
            task_id=task_id,
            script=script_score,
            video=video_score,
            video_skipped=video_skipped,
            comment=comment,
        )

    # ------------------------------------------------------------------ #
    def _evaluate_script(self, script_json: str) -> ScriptScore:
        """调用 LLM 评估脚本（4 维度）。"""
        data = self.chat_json(
            [
                {"role": "system", "content": critic_script_system_prompt()},
                {"role": "user", "content": critic_script_user_prompt(script_json)},
            ],
            temperature=0.3,
            timeout=120.0,
        )
        if not isinstance(data, dict):
            data = {}

        comments = self._as_comment_dict(data.get("comments"))
        coherence = self._clamp(data.get("coherence"))
        shootability = self._clamp(data.get("shootability"))
        character_consistency = self._clamp(data.get("character_consistency"))
        plot_completeness = self._clamp(data.get("plot_completeness"))
        total = round(
            (coherence + shootability + character_consistency + plot_completeness) / 4.0, 1
        )

        overall = str(data.get("comment") or "").strip()
        if overall:
            comments["comment"] = overall

        return ScriptScore(
            coherence=coherence,
            shootability=shootability,
            character_consistency=character_consistency,
            plot_completeness=plot_completeness,
            total=total,
            comments=comments,
        )

    def _evaluate_video(self, script_json: str, shots: list[ShotResult]) -> VideoScore:
        """调用 LLM 评估视频保真度（3 维度）。"""
        data = self.chat_json(
            [
                {"role": "system", "content": critic_video_system_prompt()},
                {
                    "role": "user",
                    "content": critic_video_user_prompt(script_json, shots_summary(shots)),
                },
            ],
            temperature=0.3,
            timeout=120.0,
        )
        if not isinstance(data, dict):
            data = {}

        comments = self._as_comment_dict(data.get("comments"))
        visual_consistency = self._clamp(data.get("visual_consistency"))
        dialogue_match = self._clamp(data.get("dialogue_match"))
        style_fidelity = self._clamp(data.get("style_fidelity"))
        total = round((visual_consistency + dialogue_match + style_fidelity) / 3.0, 1)

        overall = str(data.get("comment") or "").strip()
        if overall:
            comments["comment"] = overall

        return VideoScore(
            visual_consistency=visual_consistency,
            dialogue_match=dialogue_match,
            style_fidelity=style_fidelity,
            total=total,
            comments=comments,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _clamp(value: Any) -> float:
        """把任意输入夹取到 [1, 10] 区间并保留一位小数。"""
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 5.0
        return round(max(1.0, min(10.0, number)), 1)

    @staticmethod
    def _as_comment_dict(value: Any) -> dict[str, str]:
        """把评语字段规范化为 ``{维度: 评语}``。"""
        if not isinstance(value, dict):
            return {}
        return {str(key): str(item) for key, item in value.items()}
