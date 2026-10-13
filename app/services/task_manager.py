"""TaskManager：内存任务表 + 后台线程 + 三套状态机（任务/阶段/镜头）。

- 任务表用 ``threading.RLock`` 保护读写；
- 长流水线在后台线程中执行（由路由层启动 ``threading.Thread(daemon=True)``）；
- 单镜头失败不阻断流程；仅当**全部镜头失败**时任务级才置 failed；
- 增量：新增 ``story`` 阶段（置于最前）与剪辑导出（``export_edit_plan``）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from app.agents.critic_agent import CriticAgent
from app.agents.director_agent import DirectorAgent
from app.agents.script_agent import ScriptAgent
from app.agents.story_agent import StoryAgent
from app.models.enums import ShotState, TaskState
from app.models.schemas import (
    ClipEdit,
    Config,
    ConfirmScriptRequest,
    EvaluationReport,
    ExportRequest,
    PipelineRunRequest,
    ScriptDocument,
    ShotResult,
    StageStatus,
    StoryDocument,
    TaskStatus,
)
from app.services.first_frame_chain import FirstFrameChain
from app.services.llm_client import build_llm_client
from app.services.video_generator import VideoGenerator
from app.utils.ffmpeg_utils import concat_videos, export_from_edl, probe_duration
from app.utils.logger import format_task_log, get_logger
from app.utils.paths import Paths
from app.utils.responses import ErrorCode, err_msg

__all__ = ["TaskManager", "StageName", "NormalizedClip"]

_log = get_logger("task")


class StageName:
    """流水线阶段名常量。"""

    STORY = "story"
    SCRIPT = "script"
    DIRECTOR = "director"
    CONCAT = "concat"
    CRITIC = "critic"

    ALL: tuple[str, ...] = ("story", "script", "director", "concat", "critic")


class NormalizedClip(NamedTuple):
    """归一化后的单个可导出片段（增量：新增第 5 位 ``clip_id``）。

    字段顺序刻意保持 ``(shot_id, video_path, start, end, clip_id)``：
    **前 4 位与旧 4 元组逐字一致**，护既有 ``result[0][0..3]`` 按位断言
    （见 ``tests/test_clip_normalize.py``）。
    """

    shot_id: str              # 关联镜头 ID（对应 ``ShotResult.shot_id``）
    video_path: str           # 相对任务目录的源片段路径
    start: float              # 裁剪起点（秒）
    end: float | None         # 裁剪终点（秒）；None = 整段不裁剪
    clip_id: str | None       # 有效 id 的显式部分（split 段唯一）；缺省 None


@dataclass
class _TaskRecord:
    """任务内部记录（不对外暴露绝对路径）。"""

    task_id: str
    directory: Path
    config: Config
    request: PipelineRunRequest | None = None
    story: StoryDocument | None = None
    script: ScriptDocument | None = None
    report: EvaluationReport | None = None
    shots: list[ShotResult] = field(default_factory=list)
    chain: FirstFrameChain | None = None   # 串行首帧链编排器（director 阶段内）


class TaskManager:
    """任务管理器（单进程内存实现，进程重启后任务表清空）。"""

    def __init__(self) -> None:
        """初始化空任务表。"""
        self._records: dict[str, _TaskRecord] = {}
        self._status: dict[str, TaskStatus] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def create_task(self, directory: str | Path, config: Config | None = None) -> str:
        """创建任务并初始化状态机。

        Args:
            directory: 任务输出目录（绝对路径）。
            config: 任务使用的配置快照。

        Returns:
            新任务 ID。
        """
        task_id = uuid.uuid4().hex[:12]
        abs_dir = Path(directory).resolve()
        with self._lock:
            self._records[task_id] = _TaskRecord(
                task_id=task_id,
                directory=abs_dir,
                config=config or Config(),
            )
            self._status[task_id] = TaskStatus(
                task_id=task_id,
                state=TaskState.PENDING,
                output_dir=Paths.to_rel_outputs(abs_dir),
                stages={name: StageStatus(name=name) for name in StageName.ALL},
                shots=[],
                logs=[],
                started_at=time.time(),
            )
        return task_id

    def set_request(self, task_id: str, request: PipelineRunRequest, config: Config) -> None:
        """记录任务的请求参数与配置快照。"""
        with self._lock:
            record = self._records.get(task_id)
            if record is not None:
                record.request = request
                record.config = config

    def exists(self, task_id: str) -> bool:
        """任务是否存在。"""
        with self._lock:
            return task_id in self._records

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get(self, task_id: str) -> TaskStatus | None:
        """返回任务状态深拷贝（供 API 序列化）。"""
        with self._lock:
            status = self._status.get(task_id)
            return status.model_copy(deep=True) if status else None

    def get_output_dir(self, task_id: str) -> Path | None:
        """返回任务的输出目录绝对路径。"""
        with self._lock:
            record = self._records.get(task_id)
            return record.directory if record else None

    def get_script(self, task_id: str) -> ScriptDocument | None:
        """返回任务脚本（未生成则为 None）。"""
        with self._lock:
            record = self._records.get(task_id)
            return record.script.model_copy(deep=True) if record and record.script else None

    def get_story(self, task_id: str) -> StoryDocument | None:
        """返回任务补全后的剧情（未生成/已跳过则为 None）。"""
        with self._lock:
            record = self._records.get(task_id)
            return record.story.model_copy(deep=True) if record and record.story else None

    def get_report(self, task_id: str) -> EvaluationReport | None:
        """返回任务评估报告（未生成为 None）。"""
        with self._lock:
            record = self._records.get(task_id)
            return record.report.model_copy(deep=True) if record and record.report else None

    def get_config(self, task_id: str) -> Config | None:
        """返回任务配置快照。"""
        with self._lock:
            record = self._records.get(task_id)
            return record.config.model_copy(deep=True) if record else None

    # ------------------------------------------------------------------ #
    # 状态更新
    # ------------------------------------------------------------------ #
    def set_state(self, task_id: str, state: TaskState, error: str | None = None) -> None:
        """更新任务级状态。"""
        with self._lock:
            status = self._status.get(task_id)
            if status is None:
                return
            status.state = state
            if error is not None:
                status.error = error
            if state.is_terminal:
                status.finished_at = time.time()

    def update_stage(
        self,
        task_id: str,
        stage: str,
        state: TaskState,
        detail: str = "",
        elapsed: float | None = None,
    ) -> None:
        """更新阶段级状态。

        Args:
            task_id: 任务 ID。
            stage: 阶段名（script/director/concat/critic）。
            state: 阶段状态。
            detail: 阶段说明（如 ``shot_003/006``）。
            elapsed: 阶段耗时；为 None 时保留原值。
        """
        with self._lock:
            status = self._status.get(task_id)
            if status is None:
                return
            stage_status = status.stages.get(stage) or StageStatus(name=stage)
            stage_status.state = state
            if detail:
                stage_status.detail = detail
            if elapsed is not None:
                stage_status.elapsed = round(elapsed, 2)
            status.stages[stage] = stage_status

    def set_shots(self, task_id: str, shots: list[ShotResult]) -> None:
        """整体替换镜头列表（DirectorAgent 启动前初始化为 waiting）。"""
        with self._lock:
            status = self._status.get(task_id)
            record = self._records.get(task_id)
            if status is None or record is None:
                return
            status.shots = [shot.model_copy(deep=True) for shot in shots]
            record.shots = [shot.model_copy(deep=True) for shot in shots]

    def update_shot(self, task_id: str, shot: ShotResult) -> None:
        """按 ``shot_id`` 更新或追加单个镜头状态。"""
        with self._lock:
            status = self._status.get(task_id)
            record = self._records.get(task_id)
            if status is None or record is None:
                return
            for index, existing in enumerate(status.shots):
                if existing.shot_id == shot.shot_id:
                    status.shots[index] = shot.model_copy(deep=True)
                    break
            else:
                status.shots.append(shot.model_copy(deep=True))

            for index, existing in enumerate(record.shots):
                if existing.shot_id == shot.shot_id:
                    record.shots[index] = shot.model_copy(deep=True)
                    break
            else:
                record.shots.append(shot.model_copy(deep=True))

    def append_log(self, task_id: str, message: str) -> None:
        """追加一条供前端展示的日志行。"""
        line = format_task_log(message)
        with self._lock:
            status = self._status.get(task_id)
            if status is None:
                return
            status.logs.append(line)
            if len(status.logs) > 500:
                del status.logs[: len(status.logs) - 500]

    # ------------------------------------------------------------------ #
    # 落盘工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        """以 UTF-8 写出 JSON 文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _fail(self, task_id: str, message: str) -> None:
        """把任务置为失败并记录日志。"""
        self.append_log(task_id, message)
        self.set_state(task_id, TaskState.FAILED, message)

    # ------------------------------------------------------------------ #
    # 核心流水线
    # ------------------------------------------------------------------ #
    def run_pipeline(self, task_id: str, request: PipelineRunRequest, config: Config) -> None:
        """在后台线程中执行完整流水线：story → script → director → concat → critic。

        Args:
            task_id: 任务 ID。
            request: 运行请求参数。
            config: 配置快照。
        """
        record = self._records.get(task_id)
        if record is None:
            return
        out_dir = record.directory
        self.set_request(task_id, request, config)

        try:
            self.set_state(task_id, TaskState.RUNNING)
            self.append_log(task_id, "任务开始，正在补全故事情节…")
            llm = build_llm_client(config)

            # ---------------- 阶段 1：StoryAgent（剧情补全） ---------------- #
            self.update_stage(task_id, StageName.STORY, TaskState.RUNNING, "调用 LLM 补全剧情")
            started = time.perf_counter()
            story: StoryDocument | None = None
            try:
                if request.story is not None:
                    story = request.story
                elif request.skip_story:
                    story = None
                else:
                    story = StoryAgent(llm).run(request.dialogue, request.style)
            except Exception as exc:
                elapsed = time.perf_counter() - started
                self.update_stage(task_id, StageName.STORY, TaskState.FAILED, str(exc), elapsed)
                _log.error("[pipeline:%s] 剧情补全失败：%s", task_id, exc)
                self._fail(task_id, f"剧情补全失败：{exc}")
                return

            elapsed = time.perf_counter() - started
            record.story = story
            if story is not None:
                self.update_stage(
                    task_id,
                    StageName.STORY,
                    TaskState.SUCCESS,
                    f"共 {len(story.beats)} 个情节点",
                    elapsed,
                )
                self._write_json(out_dir / "story.json", story.model_dump(mode="json"))
                self.append_log(
                    task_id, f"已补全剧情《{story.title}》，共 {len(story.beats)} 个情节点"
                )
            else:
                self.update_stage(task_id, StageName.STORY, TaskState.SUCCESS, "已跳过", elapsed)
                self.append_log(task_id, "已跳过剧情补全，直接生成脚本")

            # ---------------- 阶段 2：ScriptAgent ---------------- #
            self.update_stage(task_id, StageName.SCRIPT, TaskState.RUNNING, "调用 LLM 生成脚本")
            started = time.perf_counter()
            try:
                script = ScriptAgent(llm).run(
                    story or request.dialogue,
                    request.style,
                    request.shot_count,
                    request.shot_duration,
                )
            except Exception as exc:
                elapsed = time.perf_counter() - started
                self.update_stage(task_id, StageName.SCRIPT, TaskState.FAILED, str(exc), elapsed)
                _log.error("[pipeline:%s] 脚本生成失败：%s", task_id, exc)
                self._fail(task_id, f"脚本生成失败：{exc}")
                return

            elapsed = time.perf_counter() - started
            record.script = script
            self.update_stage(
                task_id,
                StageName.SCRIPT,
                TaskState.SUCCESS,
                f"共 {len(script.shots)} 个镜头",
                elapsed,
            )
            self._write_json(out_dir / "script.json", script.model_dump(mode="json"))
            self.append_log(task_id, f"已生成脚本《{script.title}》，共 {len(script.shots)} 个镜头")

            # ---------------- 人工确认门（review=true 时挂起等待确认） ---------------- #
            if request.review:
                self.set_state(task_id, TaskState.AWAITING_REVIEW)
                self.append_log(task_id, "脚本已生成，等待人工确认分镜后再生成视频")
                return

            # ---------------- 阶段 3：director（等待初始图 + 逐段确认） ---------------- #
            self._init_director_stage(task_id)
        except Exception as exc:  # 兜底：任何未预期异常都不让后台线程静默死亡
            _log.exception("[pipeline:%s] 任务异常", task_id)
            self._fail(task_id, f"任务异常：{exc}")

    # ------------------------------------------------------------------ #
    # 阶段 3：导演（串行首帧链：等待初始图 → 逐段确认 → 收尾）
    # ------------------------------------------------------------------ #
    def _init_director_stage(self, task_id: str) -> None:
        """进入 director 阶段：置 RUNNING、初始化 ``record.shots`` 为段列表，等待上传初始图。

        本方法**不生成任何视频**——真正的逐段生成由 ``start_chain`` 落盘初始图后，
        由 :class:`FirstFrameChain` 逐段提交 / 接受驱动。
        """
        record = self._records.get(task_id)
        if record is None or record.script is None:
            return
        script = record.script
        total = len(script.shots)
        self.set_shots(
            task_id,
            [ShotResult(shot_id=shot.shot_id, state=ShotState.WAITING) for shot in script.shots],
        )
        self.update_stage(
            task_id, StageName.DIRECTOR, TaskState.RUNNING, f"等待上传初始图（共 {total} 段）"
        )
        self.append_log(task_id, "脚本已就绪，请上传初始图开始逐段生成")

    def _build_chain(self, task_id: str) -> FirstFrameChain | None:
        """按任务记录构建（或复用）串行首帧链编排器。"""
        with self._lock:
            record = self._records.get(task_id)
            if record is None or record.script is None:
                return None
            if record.chain is None:
                director = DirectorAgent(
                    build_llm_client(record.config), VideoGenerator(record.config)
                )
                record.chain = FirstFrameChain(
                    task_id=task_id,
                    output_dir=record.directory,
                    config=record.config,
                    script=record.script,
                    video_generator=VideoGenerator(record.config),
                    director=director,
                    on_log=lambda message: self.append_log(task_id, message),
                )
            return record.chain

    # ------------------------------------------------------------------ #
    # 串行首帧链（chain）对外接口
    # ------------------------------------------------------------------ #
    def start_chain(self, task_id: str, initial_frame_bytes: bytes) -> dict:
        """落盘初始图并启动链（自动提交段 1）。"""
        chain = self._build_chain(task_id)
        if chain is None:
            raise KeyError(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
        return chain.start(initial_frame_bytes)

    def get_chain(self, task_id: str) -> dict:
        """返回链状态（未初始化时返回空段列表）。"""
        chain = self._build_chain(task_id)
        if chain is None:
            return {"segments": [], "current_index": 0}
        return chain.get_state()

    def get_segment(self, task_id: str, index: int) -> dict:
        """返回单段详情。"""
        chain = self._build_chain(task_id)
        if chain is None:
            raise KeyError(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
        return chain.get_segment(index)

    def submit_segment(
        self,
        task_id: str,
        index: int,
        prompt: str | None = None,
        seed: int | None = None,
    ) -> dict:
        """显式提交某段（幂等；断点续跑用）。"""
        chain = self._build_chain(task_id)
        if chain is None:
            raise KeyError(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
        return chain.submit_segment(index, prompt, seed)

    def accept_segment(self, task_id: str, index: int) -> dict:
        """接受本段 → 自动提交下一段；最后一段触发 ``_finalize_chain``。"""
        chain = self._build_chain(task_id)
        if chain is None:
            raise KeyError(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
        state = chain.accept_segment(index)
        if chain.all_accepted():
            thread = threading.Thread(
                target=self._finalize_chain,
                args=(task_id,),
                daemon=True,
                name=f"storyforge-finalize-{task_id}",
            )
            thread.start()
        return state

    def regenerate_segment(
        self, task_id: str, index: int, prompt: str | None = None
    ) -> dict:
        """重生成某段（自动换随机 seed）。"""
        chain = self._build_chain(task_id)
        if chain is None:
            raise KeyError(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
        return chain.regenerate_segment(index, prompt)

    # ------------------------------------------------------------------ #
    # 收尾：director SUCCESS → concat → critic
    # ------------------------------------------------------------------ #
    def _finalize_chain(self, task_id: str) -> None:
        """首帧链全部接受后，复用既有 concat/critic 逻辑收尾成片。"""
        record = self._records.get(task_id)
        if record is None:
            return
        chain = record.chain
        if chain is None:
            return
        script = record.script
        config = record.config
        request = record.request
        out_dir = record.directory
        if request is None or script is None:
            return

        state = chain.get_state()
        segments = state.get("segments", [])
        accepted = [s for s in segments if s.get("state") == "accepted"]
        self._sync_shots_from_chain(task_id, segments)

        self.update_stage(
            task_id,
            StageName.DIRECTOR,
            TaskState.SUCCESS if accepted else TaskState.FAILED,
            f"{len(accepted)}/{len(segments)} 段成功",
        )
        self.append_log(task_id, f"首帧链完成：接受 {len(accepted)}/{len(segments)} 段")

        if not accepted:
            self._fail(task_id, "没有已接受的片段，任务终止")
            return

        # ---------------- 阶段 4：FFmpeg 拼接 ---------------- #
        self.update_stage(task_id, StageName.CONCAT, TaskState.RUNNING, "FFmpeg 拼接中")
        started = time.perf_counter()
        segment_paths = [str(out_dir / s["video_path"]) for s in accepted if s.get("video_path")]
        try:
            final_path = concat_videos(segment_paths, str(out_dir / "final_video.mp4"))
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.update_stage(task_id, StageName.CONCAT, TaskState.FAILED, str(exc), elapsed)
            self._fail(task_id, f"视频拼接失败：{exc}")
            return

        elapsed = time.perf_counter() - started
        duration = probe_duration(final_path)
        self.update_stage(
            task_id, StageName.CONCAT, TaskState.SUCCESS, Path(final_path).name, elapsed
        )
        self.append_log(task_id, f"视频拼接完成：final_video.mp4（约 {duration:.1f}s）")

        # ---------------- 阶段 5：CriticAgent ---------------- #
        shots = self.get(task_id).shots if self.get(task_id) else []
        self.update_stage(task_id, StageName.CRITIC, TaskState.RUNNING, "质量评估中")
        started = time.perf_counter()
        try:
            report = CriticAgent(build_llm_client(config)).run(
                script, shots, request.skip_video_eval, task_id=task_id
            )
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.update_stage(task_id, StageName.CRITIC, TaskState.FAILED, str(exc), elapsed)
            self.append_log(task_id, f"评估失败（不影响已生成成片）：{exc}")
        else:
            elapsed = time.perf_counter() - started
            record.report = report
            self.update_stage(task_id, StageName.CRITIC, TaskState.SUCCESS, "报告已生成", elapsed)
            self._write_json(
                out_dir / "evaluation_report.json", report.model_dump(mode="json")
            )
            self.append_log(task_id, "评估报告已生成")

        self.set_state(task_id, TaskState.SUCCESS)
        self.append_log(task_id, "任务全部完成")

    def _sync_shots_from_chain(self, task_id: str, segments: list[dict]) -> None:
        """把链的段状态映射回 ``record.shots``（供剪辑区 / 评估报告展示）。"""
        record = self._records.get(task_id)
        if record is None or record.script is None:
            return
        shots: list[ShotResult] = []
        for seg in segments:
            index = seg.get("index", 0)
            shot = (
                record.script.shots[index - 1]
                if 0 < index <= len(record.script.shots)
                else None
            )
            shot_id = shot.shot_id if shot is not None else f"segment_{index:03d}"
            state = seg.get("state")
            if state == "accepted":
                mapped_state = ShotState.SUCCESS
            elif state == "failed":
                mapped_state = ShotState.FAILED
            else:
                mapped_state = ShotState.WAITING
            shots.append(
                ShotResult(
                    shot_id=shot_id,
                    state=mapped_state,
                    video_path=seg.get("video_path"),
                    frame_path=seg.get("tail_frame_path"),
                    error=seg.get("error"),
                )
            )
        self.set_shots(task_id, shots)

    # ------------------------------------------------------------------ #
    # 分镜确认（人审门）与原地续跑
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_script(script: ScriptDocument) -> None:
        """校验人工编辑后的分镜脚本（对齐 PRD §2.5 / AC10 / AC11）。

        校验不通过时抛出携带 ``CFG_INVALID`` 的 :class:`KeyError`；**调用方需在
        状态迁移之前调用**，从而保证校验失败时任务仍停留在 ``awaiting_review``。

        Args:
            script: 待校验的脚本。

        Raises:
            KeyError: ``shot.duration`` 越界（须 2–10 秒）或 ``shot.prompt`` 为空。
        """
        for index, shot in enumerate(script.shots, start=1):
            label = shot.shot_id or f"shot_{index:03d}"
            if shot.duration < 2 or shot.duration > 10:
                raise KeyError(
                    err_msg(ErrorCode.CFG_INVALID, f"{label} 的时长须为 2–10 秒")
                )
            if not shot.prompt.strip():
                raise KeyError(
                    err_msg(ErrorCode.CFG_INVALID, f"{label} 的分镜提示词不能为空")
                )

    def confirm_script(
        self,
        task_id: str,
        request: ConfirmScriptRequest,
    ) -> tuple[str, bool]:
        """确认分镜并**原地续跑**同一 ``task_id``（D3）。

        判定（对齐设计 §2.2）：
        - ``story`` 提供且与 ``record.story`` **不等** → ``rerun_script=True``
          （忽略本次 ``shots`` 编辑，用新剧情重跑 ``ScriptAgent``）；
        - 否则若 ``script`` 提供 → 校验通过后采用该脚本、``rerun_script=False``；
        - 否则用现有脚本、``rerun_script=False``。

        并发安全（R10）：状态迁移 ``awaiting_review → running`` 在 ``RLock`` 内
        **原子**完成，后到的重复确认将看到 ``running`` 并返回 ``TASK_STATE_INVALID``。

        Args:
            task_id: 任务 ID。
            request: 确认请求（``story`` / ``script`` 均可选）。

        Returns:
            ``(state, rerun_script)``，``state`` 恒为 ``"running"``（续跑已启动）。

        Raises:
            KeyError: 任务不存在（``TASK_NOT_FOUND``）、非待确认状态
                （``TASK_STATE_INVALID``）或脚本校验失败（``CFG_INVALID``）。
        """
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                raise KeyError(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
            status = self._status.get(task_id)
            if status is None or status.state != TaskState.AWAITING_REVIEW:
                raise KeyError(
                    err_msg(ErrorCode.TASK_STATE_INVALID, "任务不在待确认状态")
                )

            new_story = request.story if request is not None else None
            rerun_script = new_story is not None and new_story != record.story

            # —— 先校验后落盘：任何 CFG_INVALID 都不改变任务状态 —— #
            if not rerun_script and request is not None and request.script is not None:
                self._validate_script(request.script)

            out_dir = record.directory
            if rerun_script:
                story = new_story.model_copy(deep=True)
                record.story = story
                self._write_json(out_dir / "story.json", story.model_dump(mode="json"))
            elif request is not None and request.script is not None:
                script = request.script.model_copy(deep=True)
                record.script = script
                self._write_json(out_dir / "script.json", script.model_dump(mode="json"))

            config = record.config
            request_snapshot = record.request
            # —— 原子状态迁移：awaiting_review → running —— #
            self.set_state(task_id, TaskState.RUNNING)

        thread = threading.Thread(
            target=self.resume_after_review,
            args=(task_id, request_snapshot, config, rerun_script),
            daemon=True,
            name=f"storyforge-resume-{task_id}",
        )
        thread.start()
        return TaskState.RUNNING.value, rerun_script

    def resume_after_review(
        self,
        task_id: str,
        request: PipelineRunRequest | None,
        config: Config,
        rerun_script: bool,
    ) -> None:
        """确认后在后台线程原地续跑（``story`` 变更时先重跑脚本，再走 3–5 阶段）。

        Args:
            task_id: 任务 ID。
            request: 首跑的运行请求快照（提供风格 / 镜头数 / 时长等参数）。
            config: 配置快照。
            rerun_script: 是否需要按新剧情重跑 ``ScriptAgent``。
        """
        record = self._records.get(task_id)
        if record is None or request is None:
            return
        out_dir = record.directory
        try:
            llm = build_llm_client(config)
            if rerun_script:
                self.update_stage(
                    task_id, StageName.SCRIPT, TaskState.RUNNING, "剧情已修改，重跑脚本"
                )
                started = time.perf_counter()
                try:
                    script = ScriptAgent(llm).run(
                        record.story or request.dialogue,
                        request.style,
                        request.shot_count,
                        request.shot_duration,
                    )
                except Exception as exc:
                    elapsed = time.perf_counter() - started
                    self.update_stage(
                        task_id, StageName.SCRIPT, TaskState.FAILED, str(exc), elapsed
                    )
                    _log.error("[pipeline:%s] 重跑脚本失败：%s", task_id, exc)
                    self._fail(task_id, f"脚本生成失败：{exc}")
                    return

                elapsed = time.perf_counter() - started
                record.script = script
                self.update_stage(
                    task_id,
                    StageName.SCRIPT,
                    TaskState.SUCCESS,
                    f"共 {len(script.shots)} 个镜头",
                    elapsed,
                )
                self._write_json(out_dir / "script.json", script.model_dump(mode="json"))
                self.append_log(
                    task_id,
                    f"已按修改后的剧情重跑脚本《{script.title}》，共 {len(script.shots)} 个镜头",
                )
            else:
                self.append_log(task_id, "已确认分镜，开始生成视频")

            self._init_director_stage(task_id)
        except Exception as exc:  # 兜底：续跑线程不得静默死亡
            _log.exception("[pipeline:%s] 确认续跑异常", task_id)
            self._fail(task_id, f"任务异常：{exc}")

    # ------------------------------------------------------------------ #
    # 剪辑导出
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_clips(
        clips: list[ClipEdit],
        shots: list[ShotResult],
    ) -> list[NormalizedClip]:
        """把前端 EDL 归一化为可导出的片段列表（纯函数）。

        规则（对齐设计 §3）：
        - 仅保留**成功且有 video_path** 的镜头；片段引用缺失/失败镜头一律丢弃；
        - **有效 id = ``clip_id or shot_id``**：缺省 ``clip_id`` 时退化为旧 ``shot_id``
          语义（逐字兼容）；同一有效 id 去重（保留首个）；
        - split 支持：同一 ``shot_id`` 的多段（``clip_id`` 互异）均保留；
        - 顺序：若任一 clip 提供了 ``order``，先按 ``(order is None, order)`` 稳定排序，
          再取数组顺序；**未被任一 clip（含禁用）引用的可用镜头**按其原始顺序**追加**
          到末尾（宽容），覆盖判据为 ``covered_shot_ids``（shot 维度、只记一次，
          避免 split 场景产生幽灵片段）；
        - ``start < 0`` 钳制为 0；``end is None`` 取真实时长；``end > duration`` 收敛到
          ``duration``；``end <= start``、``start >= duration``、``duration <= 0`` 一律
          回退**该段整段**（不裁剪）。

        Args:
            clips: 有序剪辑指令列表。
            shots: 任务镜头结果列表。

        Returns:
            ``list[NormalizedClip]``；``end`` 为 ``None`` 表示整段不裁剪。
            返回值前 4 位与旧 4 元组逐字一致（护既有按位断言）。
        """
        available: dict[str, ShotResult] = {
            shot.shot_id: shot
            for shot in shots
            if shot.state == ShotState.SUCCESS and shot.video_path
        }

        ordered = list(clips)
        if any(clip.order is not None for clip in ordered):
            ordered.sort(key=lambda c: (c.order is None, c.order if c.order is not None else 0))

        result: list[NormalizedClip] = []
        seen_clip_ids: set[str] = set()
        covered_shot_ids: set[str] = set()
        for clip in ordered:
            shot_id = clip.shot_id
            eff_id = clip.clip_id or shot_id
            if not shot_id or shot_id not in available:
                continue
            if eff_id in seen_clip_ids:
                continue
            seen_clip_ids.add(eff_id)
            covered_shot_ids.add(shot_id)  # 命中即「已覆盖」（含禁用）→ 不再被追加
            if not clip.enabled:
                continue
            shot = available[shot_id]

            duration = max(0.0, float(shot.duration or 0.0))
            if duration <= 0.0:
                # 时长探测失败：整段、不裁剪（探测失败不丢片、不阻断导出）
                result.append(NormalizedClip(shot_id, str(shot.video_path), 0.0, None, clip.clip_id))
                continue

            start = float(clip.start or 0.0)
            if start < 0.0:
                start = 0.0
            end = clip.end
            end = duration if end is None else float(end)
            if end > duration:
                end = duration
            if start >= duration or end <= start:
                # 无效裁剪 → 回退整段
                start, end = 0.0, duration
            result.append(NormalizedClip(shot_id, str(shot.video_path), start, end, clip.clip_id))

        # 未被任一 clip 引用的可用镜头：按原始顺序追加（enabled=True、不裁剪）
        for shot in shots:
            if shot.shot_id in covered_shot_ids:
                continue
            if shot.state != ShotState.SUCCESS or not shot.video_path:
                continue
            covered_shot_ids.add(shot.shot_id)
            duration = max(0.0, float(shot.duration or 0.0))
            result.append(
                NormalizedClip(
                    shot.shot_id,
                    str(shot.video_path),
                    0.0,
                    duration if duration > 0.0 else None,
                    None,
                )
            )
        return result

    def export_edit_plan(
        self,
        task_id: str,
        request: ExportRequest,
    ) -> tuple[str, str, int, float]:
        """按 EDL 裁剪并拼接为新成片，同时落盘配对命名并落盘剪辑方案。

        产物（同一时间戳配对命名，避免多次导出互相覆盖）：
        - 成片：``outputs/<ts>/exports/export_<YYYYMMDD_HHMMSS>.mp4``
        - 方案：``outputs/<ts>/exports/edit_plan_<同一个 YYYYMMDD_HHMMSS>.json``

        Args:
            task_id: 任务 ID。
            request: 导出请求（有序剪辑方案）。

        Returns:
            ``(export_path, edit_plan_path, clip_count, total_duration)``，
            其中两个路径均为**任务相对正斜杠路径**。

        Raises:
            KeyError: 任务不存在、脚本/成功镜头缺失或无可导出片段（携带错误码）。
            FFmpegError: 裁剪/拼接失败。
        """
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                raise KeyError(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
            if record.script is None:
                raise KeyError(err_msg(ErrorCode.TASK_FAILED, "脚本尚未生成，无法导出剪辑"))
            shots = [shot.model_copy(deep=True) for shot in record.shots]
            out_dir = record.directory

        clips = request.clips if request else []
        normalized = self._normalize_clips(clips, shots)
        if not normalized:
            raise KeyError(err_msg(ErrorCode.SYS_BAD_REQUEST, "没有可导出的片段"))

        exports_dir = out_dir / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_abs = exports_dir / f"export_{stamp}.mp4"
        index = 1
        while export_abs.exists():
            export_abs = exports_dir / f"export_{stamp}_{index}.mp4"
            index += 1

        # 配对命名：edit_plan_<与成片完全相同的后缀>.json
        plan_stamp = export_abs.stem[len("export_"):]
        plan_abs = exports_dir / f"edit_plan_{plan_stamp}.json"

        clip_inputs = [
            (str(out_dir / video_path), start, end)
            for (_shot_id, video_path, start, end, _clip_id) in normalized
        ]

        export_from_edl(clip_inputs, str(export_abs))

        expected_duration = 0.0
        plan_clips: list[dict] = []
        for shot_id, video_path, start, end, clip_id in normalized:
            if start <= 0.0 and end is None:
                segment = probe_duration(str(out_dir / video_path))
            else:
                segment = max(0.0, float(end) - float(start))
            expected_duration += segment
            plan_clips.append(
                {
                    "shot_id": shot_id,
                    "clip_id": clip_id,
                    "source": video_path.replace("\\", "/"),
                    "start": round(start, 3),
                    "end": round(end, 3) if end is not None else None,
                    "duration": round(segment, 3),
                }
            )

        plan = {
            "task_id": task_id,
            "export_file": export_abs.name,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "clip_count": len(normalized),
            "clips": plan_clips,
        }
        self._write_json(plan_abs, plan)

        actual_duration = probe_duration(str(export_abs))
        total_duration = actual_duration if actual_duration > 0.0 else expected_duration

        export_rel = export_abs.relative_to(out_dir).as_posix()
        plan_rel = plan_abs.relative_to(out_dir).as_posix()
        self.append_log(
            task_id,
            f"已导出剪辑成片：{export_rel}（{len(normalized)} 个片段，约 {total_duration:.1f}s）",
        )
        return export_rel, plan_rel, len(normalized), round(float(total_duration), 3)

    # ------------------------------------------------------------------ #
    # 剪辑方案草稿（供前端刷新后恢复）
    # ------------------------------------------------------------------ #
    DRAFT_NAME = "edit_plan_draft.json"

    def _draft_path(self, task_id: str) -> Path | None:
        """返回任务目录下的剪辑草稿文件绝对路径（任务不存在则 None）。"""
        with self._lock:
            record = self._records.get(task_id)
            return record.directory / self.DRAFT_NAME if record else None

    def save_edit_draft(self, task_id: str, request: ExportRequest) -> int:
        """落盘前端当前的剪辑方案草稿（用于刷新后恢复）。

        与 :meth:`export_edit_plan` 的区别：**不触发任何 FFmpeg 操作**，仅记录
        用户的剪辑意图（顺序 / 起止 / 启停），因此可以随每次编辑高频调用。

        Args:
            task_id: 任务 ID。
            request: 剪辑方案（EDL）。

        Returns:
            落盘的片段数。

        Raises:
            KeyError: 任务不存在（携带 ``TASK_NOT_FOUND``）。
        """
        path = self._draft_path(task_id)
        if path is None:
            raise KeyError(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))

        clips = request.clips if request else []
        payload = {
            "task_id": task_id,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "clip_count": len(clips),
            "clips": [clip.model_dump(mode="json") for clip in clips],
        }
        self._write_json(path, payload)
        return len(clips)

    def get_edit_draft(self, task_id: str) -> list[dict]:
        """读取剪辑方案草稿。

        **刻意不抛异常**：草稿属于体验优化，缺失或损坏时静默返回空列表，
        由前端退回「原始顺序、全启用、不裁剪」，绝不影响主流程。

        Args:
            task_id: 任务 ID。

        Returns:
            剪辑指令字典列表；无草稿/任务不存在/文件损坏时返回 ``[]``。
        """
        path = self._draft_path(task_id)
        if path is None or not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _log.warning("剪辑草稿读取失败，已忽略：%s", path)
            return []
        clips = payload.get("clips") if isinstance(payload, dict) else None
        return clips if isinstance(clips, list) else []
