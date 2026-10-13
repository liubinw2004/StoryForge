"""流水线路由：``/api/pipeline/*`` 启动、轮询状态、脚本、剧情、报告、重试镜头、导出剪辑。"""

from __future__ import annotations

import threading
from urllib.parse import quote

from fastapi import APIRouter, Body, File, UploadFile

from app.deps import get_config_store, get_task_manager
from app.models.schemas import (
    ConfirmScriptRequest,
    ExportRequest,
    PipelineRunRequest,
)
from app.utils.ffmpeg_utils import FFmpegError
from app.utils.logger import get_logger
from app.utils.paths import Paths
from app.utils.responses import ErrorCode, err_msg, fail, ok

__all__ = ["router"]

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])
_log = get_logger("pipeline")


@router.post("/run")
def run_pipeline(request: PipelineRunRequest) -> dict:
    """创建任务并**立即返回** ``task_id``，长流水线在后台线程执行。"""
    config = get_config_store().get_raw()
    manager = get_task_manager()

    output_dir = Paths.new_output_dir()
    task_id = manager.create_task(output_dir, config)
    manager.set_request(task_id, request, config)

    thread = threading.Thread(
        target=manager.run_pipeline,
        args=(task_id, request, config),
        daemon=True,
        name=f"storyforge-{task_id}",
    )
    thread.start()

    status = manager.get(task_id)
    output_rel = status.output_dir if status else Paths.to_rel_outputs(output_dir)
    _log.info("[pipeline:%s] 任务已启动，输出目录 %s", task_id, output_rel)
    return ok({"task_id": task_id, "output_dir": output_rel})


@router.get("/{task_id}/status")
def get_status(task_id: str) -> dict:
    """轮询任务状态（前端每 1000ms 调用一次）。"""
    status = get_task_manager().get(task_id)
    if status is None:
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
    return ok(status.model_dump(mode="json"))


@router.get("/{task_id}/script")
def get_script(task_id: str) -> dict:
    """获取脚本 JSON。"""
    script = get_task_manager().get_script(task_id)
    if script is None:
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, "脚本尚未生成或任务不存在"))
    return ok(script.model_dump(mode="json"))


@router.get("/{task_id}/story")
def get_story(task_id: str) -> dict:
    """获取补全后的剧情 JSON（StoryDocument）。"""
    story = get_task_manager().get_story(task_id)
    if story is None:
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, "剧情尚未生成或任务不存在"))
    return ok(story.model_dump(mode="json"))


@router.get("/{task_id}/report")
def get_report(task_id: str) -> dict:
    """获取评估报告 JSON。"""
    report = get_task_manager().get_report(task_id)
    if report is None:
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, "评估报告尚未生成或任务不存在"))
    return ok(report.model_dump(mode="json"))


@router.post("/{task_id}/chain/start")
def chain_start(task_id: str, file: UploadFile = File(...)) -> dict:
    """上传初始图并启动串行首帧链（自动提交段 1）。"""
    manager = get_task_manager()
    if not manager.exists(task_id):
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
    try:
        data = file.file.read()
    except Exception as exc:  # noqa: BLE001
        return fail(err_msg(ErrorCode.SYS_BAD_REQUEST, f"读取初始图失败：{exc}"))
    try:
        state = manager.start_chain(task_id, data)
    except KeyError as exc:
        return fail(str(exc.args[0]) if exc.args else "启动首帧链失败")
    except Exception as exc:  # noqa: BLE001
        _log.error("[pipeline:%s] 启动首帧链失败：%s", task_id, exc)
        return fail(err_msg(ErrorCode.TASK_FAILED, f"启动首帧链失败：{exc}"))
    return ok({"task_id": task_id, "chain": state})


@router.get("/{task_id}/chain")
def get_chain(task_id: str) -> dict:
    """返回 ``chain.json`` 内容（当前段索引 + 各段状态/产物路径/错误）。"""
    manager = get_task_manager()
    if not manager.exists(task_id):
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
    try:
        state = manager.get_chain(task_id)
    except KeyError as exc:
        return fail(str(exc.args[0]) if exc.args else "读取首帧链失败")
    return ok({"task_id": task_id, "chain": state})


@router.post("/{task_id}/chain/segments/{index}/submit")
def chain_submit_segment(
    task_id: str,
    index: int,
    body: dict | None = Body(default=None),
) -> dict:
    """显式提交某段（幂等；仅 ``pending``/``failed`` 段可提交，断点续跑用）。"""
    manager = get_task_manager()
    if not manager.exists(task_id):
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
    prompt = (body or {}).get("prompt")
    seed = (body or {}).get("seed")
    try:
        state = manager.submit_segment(task_id, index, prompt, seed)
    except KeyError as exc:
        return fail(str(exc.args[0]) if exc.args else "提交段失败")
    except Exception as exc:  # noqa: BLE001
        _log.error("[pipeline:%s] 提交段 %s 失败：%s", task_id, index, exc)
        return fail(err_msg(ErrorCode.TASK_FAILED, f"提交段失败：{exc}"))
    return ok({"task_id": task_id, "chain": state})


@router.get("/{task_id}/chain/segments/{index}")
def get_chain_segment(task_id: str, index: int) -> dict:
    """单段详情 + 视频/尾帧相对路径。"""
    manager = get_task_manager()
    if not manager.exists(task_id):
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
    try:
        segment = manager.get_segment(task_id, index)
    except KeyError as exc:
        return fail(str(exc.args[0]) if exc.args else "读取段失败")
    return ok({"task_id": task_id, "segment": segment})


@router.post("/{task_id}/chain/segments/{index}/accept")
def chain_accept_segment(task_id: str, index: int, body: dict | None = Body(default=None)) -> dict:
    """接受本段 → 锁定尾帧为下一段首帧 → 自动提交段 i+1；最后一段触发拼接收尾。"""
    manager = get_task_manager()
    if not manager.exists(task_id):
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
    try:
        state = manager.accept_segment(task_id, index)
    except KeyError as exc:
        return fail(str(exc.args[0]) if exc.args else "接受段失败")
    except Exception as exc:  # noqa: BLE001
        _log.error("[pipeline:%s] 接受段 %s 失败：%s", task_id, index, exc)
        return fail(err_msg(ErrorCode.TASK_FAILED, f"接受段失败：{exc}"))
    return ok({"task_id": task_id, "chain": state})


@router.post("/{task_id}/chain/segments/{index}/regenerate")
def chain_regenerate_segment(
    task_id: str,
    index: int,
    body: dict | None = Body(default=None),
) -> dict:
    """重生成某段（自动换随机 seed），覆盖本段产物。"""
    manager = get_task_manager()
    if not manager.exists(task_id):
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
    prompt = (body or {}).get("prompt")
    try:
        state = manager.regenerate_segment(task_id, index, prompt)
    except KeyError as exc:
        return fail(str(exc.args[0]) if exc.args else "重生成段失败")
    except Exception as exc:  # noqa: BLE001
        _log.error("[pipeline:%s] 重生成段 %s 失败：%s", task_id, index, exc)
        return fail(err_msg(ErrorCode.TASK_FAILED, f"重生成段失败：{exc}"))
    return ok({"task_id": task_id, "chain": state})


@router.post("/{task_id}/confirm-script")
def confirm_script(task_id: str, body: ConfirmScriptRequest) -> dict:
    """确认分镜并**原地续跑**同一 ``task_id``（人审门，D3）。

    请求体可携带人工编辑后的 ``story`` / ``script``：``story`` 变更时重跑
    ``ScriptAgent``，否则采用 ``script``（仅改分镜，不重跑）。成功即已在后台线程
    启动续跑，``state`` 恒返回 ``"running"``。
    """
    manager = get_task_manager()
    if not manager.exists(task_id):
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
    try:
        state, rerun_script = manager.confirm_script(task_id, body)
    except KeyError as exc:
        return fail(str(exc.args[0]) if exc.args else "确认分镜失败")
    except Exception as exc:  # noqa: BLE001
        _log.error("[pipeline:%s] 确认分镜失败：%s", task_id, exc)
        return fail(err_msg(ErrorCode.TASK_FAILED, f"确认分镜失败：{exc}"))
    return ok({"task_id": task_id, "state": state, "rerun_script": rerun_script})


@router.post("/{task_id}/export")
def export_video(task_id: str, body: ExportRequest) -> dict:
    """按前端剪辑方案（EDL）同步导出为新成片，并落盘配对命名的剪辑方案。

    产物：``exports/export_<ts>.mp4`` 与 ``exports/edit_plan_<ts>.json``（同时间戳配对）；
    **绝不覆盖** ``final_video.mp4``。返回的路径均为任务相对正斜杠路径。
    """
    manager = get_task_manager()
    if not manager.exists(task_id):
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
    try:
        export_rel, plan_rel, clip_count, total_duration = manager.export_edit_plan(task_id, body)
    except KeyError as exc:
        return fail(str(exc.args[0]) if exc.args else "导出失败")
    except FFmpegError as exc:
        _log.error("[pipeline:%s] 导出剪辑失败：%s", task_id, exc)
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _log.error("[pipeline:%s] 导出剪辑异常：%s", task_id, exc)
        return fail(err_msg(ErrorCode.FFM_EXEC_FAILED, f"导出剪辑失败：{exc}"))

    download_url = (
        f"/api/pipeline/{task_id}/download-artifact?name=" + quote(export_rel, safe="/")
    )
    return ok(
        {
            "task_id": task_id,
            "export_path": export_rel,
            "download_url": download_url,
            "edit_plan_path": plan_rel,
            "clip_count": clip_count,
            "total_duration": total_duration,
        }
    )


@router.get("/{task_id}/edit-plan")
def get_edit_plan(task_id: str) -> dict:
    """读取剪辑方案草稿（供前端刷新后恢复）。

    无草稿时返回空 ``clips`` 列表（而非报错），前端据此退回原始剪辑。
    """
    manager = get_task_manager()
    if not manager.exists(task_id):
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
    return ok({"task_id": task_id, "clips": manager.get_edit_draft(task_id)})


@router.put("/{task_id}/edit-plan")
def save_edit_plan(task_id: str, body: ExportRequest) -> dict:
    """保存剪辑方案草稿（顺序 / 起止 / 启停）。

    仅落盘 JSON，**不执行任何 FFmpeg 操作**，因此前端可在每次编辑后高频调用。
    """
    manager = get_task_manager()
    if not manager.exists(task_id):
        return fail(err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))
    try:
        count = manager.save_edit_draft(task_id, body)
    except KeyError as exc:
        return fail(str(exc.args[0]) if exc.args else "保存剪辑方案失败")
    except Exception as exc:  # noqa: BLE001
        _log.error("[pipeline:%s] 保存剪辑方案失败：%s", task_id, exc)
        return fail(err_msg(ErrorCode.TASK_FAILED, f"保存剪辑方案失败：{exc}"))
    return ok({"task_id": task_id, "clip_count": count})
