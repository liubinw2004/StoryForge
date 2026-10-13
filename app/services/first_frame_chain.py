"""串行首帧链编排器：状态机 + ``chain.json`` 断点续跑 + 全局串行锁 + 805 重试。

职责（对齐增量设计 §三/§四）：

- **严格串行**：模块级 :data:`_SUBMIT_LOCK` 跨任务全局互斥，同一时刻仅 1 个 RunningHub
  提交在飞（防 421）；锁仅覆盖「单段四步 + 尾帧抽取」，不包 UI 读。
- **逐段确认状态机**：``pending → submitting → awaiting_confirm → accepted``，
  异常分支 ``failed``；``regenerate`` 回到 ``pending``（换随机 seed 重跑）。
- **断点续跑**：``chain.json`` 每状态迁移即写盘；``current_index`` 单源；``submit`` 幂等。
- **805 重试**：命中 805 自动换随机 seed 重试 1 次，仍失败置 ``failed`` 交用户 ``regenerate``。
- 首帧来源：段 1 用用户上传的初始图；后续段用上一段抽取的尾帧（``accept`` 时锁定）。

对外统一返回**任务相对正斜杠路径**（``frames/…`` / ``shots/…``），绝不暴露绝对路径。
"""

from __future__ import annotations

import json
import random
import threading
from enum import Enum
from pathlib import Path
from typing import Callable

from app.models.schemas import Config, ScriptDocument
from app.services.video_generator import VideoError, VideoGenerator
from app.utils.ffmpeg_utils import extract_tail_frame
from app.utils.logger import get_logger
from app.utils.responses import ErrorCode, err_msg

__all__ = ["ChainSegmentState", "FirstFrameChain", "SUBMIT_LOCK"]

_log = get_logger("chain")

#: 跨任务全局串行锁（同一时刻仅 1 个 RunningHub 提交在飞，防 421 并发/队列超限）。
SUBMIT_LOCK = threading.Lock()

#: 805 失败自动重试次数上限（额外 1 次，即总共 2 次提交）。
_MAX_805_RETRIES = 1

_IN_FLIGHT_STATES = frozenset({"uploading", "submitting", "polling"})


class ChainSegmentState(str, Enum):
    """单段状态机枚举。"""

    PENDING = "pending"
    UPLOADING = "uploading"
    SUBMITTING = "submitting"
    POLLING = "polling"
    AWAITING_CONFIRM = "awaiting_confirm"
    ACCEPTED = "accepted"
    FAILED = "failed"


class FirstFrameChain:
    """串行首帧链：管理 ``chain.json`` 状态与逐段提交/接受/重生成。"""

    def __init__(
        self,
        task_id: str,
        output_dir: Path,
        config: Config,
        script: ScriptDocument,
        video_generator: VideoGenerator,
        director,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        """初始化链编排器（不立即写盘，等 :meth:`start` 传入初始图）。

        Args:
            task_id: 任务 ID。
            output_dir: 任务输出目录绝对路径。
            config: 配置快照。
            script: 分镜脚本（决定段数与各段提示词/时长）。
            video_generator: 视频生成统一入口（提供 ``generate_i2v``）。
            director: :class:`DirectorAgent`（提供 ``build_prompt``）。
            on_log: 可选日志回调（写入任务日志）。
        """
        self.task_id = task_id
        self.output_dir = Path(output_dir)
        self.config = config
        self.script = script
        self.video = video_generator
        self.director = director
        self.on_log = on_log

        self._lock = threading.RLock()
        self.path = self.output_dir / "chain.json"
        self.shots_dir = self.output_dir / "shots"
        self.frames_dir = self.output_dir / "frames"
        self._chain: dict | None = None
        self._load()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self, initial_frame_bytes: bytes) -> dict:
        """落盘初始图并初始化链，**自动提交段 1**（后台线程），返回链状态。

        幂等：链已初始化时不再重建，仅返回当前状态（避免重复覆盖已生成的段）。

        Args:
            initial_frame_bytes: 初始图 PNG 字节。

        Returns:
            链状态字典。

        Raises:
            KeyError: 初始图为空（``SYS_BAD_REQUEST``）。
        """
        if not initial_frame_bytes:
            raise KeyError(err_msg(ErrorCode.SYS_BAD_REQUEST, "初始图不能为空"))

        with self._lock:
            if self._chain is not None:
                return self._snapshot()

            self.shots_dir.mkdir(parents=True, exist_ok=True)
            self.frames_dir.mkdir(parents=True, exist_ok=True)
            initial_rel = "frames/initial_frame.png"
            (self.output_dir / initial_rel).write_bytes(initial_frame_bytes)

            segments = []
            for index, _shot in enumerate(self.script.shots, start=1):
                segments.append(
                    {
                        "index": index,
                        "state": ChainSegmentState.PENDING.value,
                        "first_frame_source": initial_rel if index == 1 else None,
                        "video_path": None,
                        "tail_frame_path": None,
                        "prompt": None,
                        "seed": None,
                        "runninghub_task_id": None,
                        "error": None,
                    }
                )
            self._chain = {
                "version": 1,
                "task_id": self.task_id,
                "workflow_id": self.config.rh_workflow_id,
                "current_index": 1,
                "initial_frame": initial_rel,
                "segments": segments,
            }
            self._save()

        self._log("串行首帧链已初始化，开始提交段 1")
        self.submit_segment(1)
        return self._snapshot()

    # ------------------------------------------------------------------ #
    # 段操作
    # ------------------------------------------------------------------ #
    def submit_segment(
        self,
        index: int,
        prompt_override: str | None = None,
        seed_override: int | None = None,
    ) -> dict:
        """显式提交某段（幂等：仅 ``pending`` / ``failed`` 段可提交）。

        Args:
            index: 段序号（1 基）。
            prompt_override: 可选提示词覆盖（缺省用导演层 ``build_prompt``）。
            seed_override: 可选种子覆盖（缺省随机）。

        Returns:
            链状态字典。
        """
        with self._lock:
            seg = self._segment(index)
            state = seg.get("state")
            if state in _IN_FLIGHT_STATES or state == ChainSegmentState.AWAITING_CONFIRM.value:
                return self._snapshot()
            if state == ChainSegmentState.ACCEPTED.value:
                return self._snapshot()

            if prompt_override is not None:
                seg["prompt"] = prompt_override
            if seg.get("prompt") is None:
                seg["prompt"] = self._default_prompt(index)
            if seed_override is not None:
                seg["seed"] = seed_override
            elif seg.get("seed") is None:
                seg["seed"] = random.randint(0, 2_147_483_647)
            seg["state"] = ChainSegmentState.PENDING.value
            seg["error"] = None
            self._save()

        self._launch(index)
        return self._snapshot()

    def accept_segment(self, index: int) -> dict:
        """接受本段 → 锁定尾帧为下一段首帧 → 自动提交段 i+1；最后一段不再提交。

        Args:
            index: 段序号（1 基）。

        Returns:
            链状态字典。

        Raises:
            KeyError: 段不存在或状态不是 ``awaiting_confirm``（``TASK_STATE_INVALID``）。
        """
        with self._lock:
            seg = self._segment(index)
            if seg.get("state") != ChainSegmentState.AWAITING_CONFIRM.value:
                raise KeyError(
                    err_msg(ErrorCode.TASK_STATE_INVALID, f"段 {index} 尚未完成，无法接受")
                )
            seg["state"] = ChainSegmentState.ACCEPTED.value
            total = len(self.script.shots)
            if index < total:
                self._chain["current_index"] = index + 1
                nxt = self._segment(index + 1)
                nxt["first_frame_source"] = seg.get("tail_frame_path")
            self._save()

        if index < total:
            self._log(f"段 {index} 已接受，开始提交段 {index + 1}")
            self.submit_segment(index + 1)
        else:
            self._log(f"段 {index} 已接受，全部段完成")
        return self._snapshot()

    def regenerate_segment(self, index: int, prompt_override: str | None = None) -> dict:
        """重生成某段（自动换随机 seed），覆盖本段产物；首帧来源不变。

        Args:
            index: 段序号（1 基）。
            prompt_override: 可选提示词覆盖。

        Returns:
            链状态字典。

        Raises:
            KeyError: 段不存在或状态不允许重生成（``TASK_STATE_INVALID``）。
        """
        with self._lock:
            seg = self._segment(index)
            if seg.get("state") not in (
                ChainSegmentState.AWAITING_CONFIRM.value,
                ChainSegmentState.FAILED.value,
            ):
                raise KeyError(
                    err_msg(
                        ErrorCode.TASK_STATE_INVALID,
                        f"段 {index} 当前状态不可重生成",
                    )
                )
            seg["prompt"] = (
                prompt_override if prompt_override is not None else self._default_prompt(index)
            )
            seg["seed"] = random.randint(0, 2_147_483_647)
            seg["state"] = ChainSegmentState.PENDING.value
            seg["error"] = None
            seg["video_path"] = None
            seg["tail_frame_path"] = None
            seg["runninghub_task_id"] = None
            self._save()

        self._log(f"段 {index} 重生成已提交")
        self._launch(index)
        return self._snapshot()

    # ------------------------------------------------------------------ #
    # 状态读取
    # ------------------------------------------------------------------ #
    def get_state(self) -> dict:
        """返回当前链状态（未初始化时返回空对象 ``{"segments": []}``）。"""
        with self._lock:
            return self._snapshot()

    def get_segment(self, index: int) -> dict:
        """返回单段详情（含视频/尾帧相对路径）。"""
        with self._lock:
            return dict(self._segment(index))

    def all_accepted(self) -> bool:
        """是否所有段均已 ``accepted``。"""
        with self._lock:
            if self._chain is None:
                return False
            return all(
                seg.get("state") == ChainSegmentState.ACCEPTED.value
                for seg in self._chain.get("segments", [])
            )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _default_prompt(self, index: int) -> str:
        """用导演层构建第 ``index`` 段的默认提示词。"""
        shot = self.script.shots[index - 1]
        total = len(self.script.shots)
        return self.director.build_prompt(shot, self.script, index, total, self.config)

    def _segment(self, index: int) -> dict:
        """取第 ``index`` 段（1 基）的字典引用。

        Raises:
            KeyError: 链未初始化或序号越界（``TASK_STATE_INVALID``）。
        """
        if self._chain is None:
            raise KeyError(err_msg(ErrorCode.TASK_STATE_INVALID, "串行首帧链尚未初始化"))
        segments = self._chain.get("segments", [])
        if index < 1 or index > len(segments):
            raise KeyError(
                err_msg(ErrorCode.TASK_STATE_INVALID, f"段序号 {index} 越界")
            )
        return segments[index - 1]

    def _launch(self, index: int) -> None:
        """启动后台线程执行单段四步 + 尾帧抽取。"""
        thread = threading.Thread(
            target=self._run_segment,
            args=(index,),
            daemon=True,
            name=f"storyforge-chain-{self.task_id}-{index}",
        )
        thread.start()

    def _run_segment(self, index: int) -> None:
        """后台线程：在全局锁内执行「生成 → 抽尾帧」，并把结果写回 ``chain.json``。"""
        with self._lock:
            seg = self._segment(index)
            first_frame_rel = seg.get("first_frame_source")
            prompt = seg.get("prompt")
            seed = seg.get("seed")
            duration = int(self.script.shots[index - 1].duration)
            size = self.config.resolution.label
            out_rel = f"shots/segment_{index:03d}.mp4"
            tail_rel = f"frames/tail_{index:03d}.png"

        if not first_frame_rel:
            self._fail_segment(index, "首帧来源缺失，无法生成本段")
            return

        first_frame_abs = str(self.output_dir / first_frame_rel)
        out_abs = str(self.output_dir / out_rel)
        tail_abs = str(self.output_dir / tail_rel)

        try:
            with SUBMIT_LOCK:
                self._set_state(index, ChainSegmentState.SUBMITTING.value)
                produced, final_seed = self._generate_with_retry(
                    index, first_frame_abs, prompt or "", duration, seed, size, out_abs
                )
                extract_tail_frame(produced, tail_abs)

            with self._lock:
                seg = self._segment(index)
                seg["video_path"] = out_rel
                seg["tail_frame_path"] = tail_rel
                seg["seed"] = final_seed
                seg["error"] = None
                seg["state"] = ChainSegmentState.AWAITING_CONFIRM.value
                self._save()
            self._log(f"段 {index} 已生成，等待确认")
        except Exception as exc:  # noqa: BLE001 - 任何失败都交用户 regenerate
            self._fail_segment(index, str(exc))

    def _generate_with_retry(
        self,
        index: int,
        first_frame_abs: str,
        prompt: str,
        duration: int,
        seed: int,
        size: str,
        out_abs: str,
    ) -> tuple[str, int]:
        """调用 ``generate_i2v``，命中 805 时换随机 seed 重试 1 次。"""
        attempt = 0
        current_seed = seed
        while True:
            attempt += 1
            try:
                produced = self.video.generate_i2v(
                    first_frame_abs, prompt, duration, current_seed, size, out_abs
                )
                return produced, current_seed
            except VideoError as exc:
                if self._is_805(str(exc)) and attempt <= _MAX_805_RETRIES:
                    self._log(f"段 {index} 命中 805，自动换 seed 重试（第 {attempt} 次）")
                    current_seed = random.randint(0, 2_147_483_647)
                    continue
                raise

    @staticmethod
    def _is_805(message: str) -> bool:
        """判断错误文本是否命中 RunningHub 805（任务失败，可换 seed 重试）。"""
        return "805" in (message or "")

    def _set_state(self, index: int, state: str) -> None:
        """原子更新段状态并写盘。"""
        with self._lock:
            seg = self._segment(index)
            seg["state"] = state
            self._save()

    def _fail_segment(self, index: int, error: str) -> None:
        """把段置为 ``failed`` 并写盘。"""
        with self._lock:
            seg = self._segment(index)
            seg["state"] = ChainSegmentState.FAILED.value
            seg["error"] = error
            self._save()
        self._log(f"段 {index} 生成失败：{error}")

    def _snapshot(self) -> dict:
        """返回链状态的深拷贝（未初始化时返回空对象）。"""
        if self._chain is None:
            return {"segments": [], "current_index": 0}
        return json.loads(json.dumps(self._chain, ensure_ascii=False))

    def _load(self) -> None:
        """从磁盘加载 ``chain.json``（断点续跑）；不存在/损坏时置空。"""
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self._chain = payload
        except (OSError, ValueError):
            _log.warning("chain.json 读取失败，已忽略：%s", self.path)

    def _save(self) -> None:
        """把当前链状态以 UTF-8 写回磁盘。"""
        if self._chain is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._chain, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _log(self, message: str) -> None:
        """写模块日志 + 任务日志回调。"""
        _log.info("[chain:%s] %s", self.task_id, message)
        if self.on_log:
            self.on_log(message)
