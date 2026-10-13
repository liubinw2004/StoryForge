"""结果路由：视频 / 抽帧图片的流式访问与附件下载（含路径穿越校验）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.deps import get_task_manager
from app.utils.paths import Paths
from app.utils.responses import ErrorCode, err_msg

__all__ = ["router"]

router = APIRouter(prefix="/api/pipeline", tags=["result"])

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".json": "application/json",
    ".txt": "text/plain",
}


def _resolve_task_file(task_id: str, name: str):
    """在任务目录内安全解析文件路径。

    Args:
        task_id: 任务 ID。
        name: 相对任务目录的文件名（允许 ``shots/xxx.mp4``）。

    Returns:
        绝对路径。

    Raises:
        HTTPException: 任务不存在、路径非法或文件不存在。
    """
    manager = get_task_manager()
    base = manager.get_output_dir(task_id)
    if base is None:
        raise HTTPException(status_code=404, detail=err_msg(ErrorCode.TASK_NOT_FOUND, f"任务 {task_id} 不存在"))

    try:
        target = Paths.safe_join(base, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=err_msg(ErrorCode.SYS_BAD_REQUEST, str(exc))) from exc

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=err_msg(ErrorCode.TASK_NOT_FOUND, f"文件不存在：{name}"))
    return target


@router.get("/{task_id}/video/{name:path}")
def get_video(task_id: str, name: str):
    """返回视频流（``final_video.mp4`` 或 ``shots/shot_00N.mp4``）。"""
    target = _resolve_task_file(task_id, name)
    suffix = target.suffix.lower()
    if suffix != ".mp4":
        raise HTTPException(status_code=400, detail=err_msg(ErrorCode.SYS_BAD_REQUEST, "仅支持访问 mp4 视频"))
    return FileResponse(str(target), media_type="video/mp4", filename=target.name)


@router.get("/{task_id}/frames/{name}")
def get_frame(task_id: str, name: str):
    """返回末帧参考图（``frames/frame_00N.jpg``）。"""
    target = _resolve_task_file(task_id, f"frames/{name}")
    suffix = target.suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png"):
        raise HTTPException(status_code=400, detail=err_msg(ErrorCode.SYS_BAD_REQUEST, "仅支持访问图片文件"))
    media_type = _MEDIA_TYPES.get(suffix, "application/octet-stream")
    return FileResponse(str(target), media_type=media_type, filename=target.name)


@router.get("/{task_id}/download")
def download_final(task_id: str):
    """下载最终成片（``Content-Disposition: attachment``）。"""
    target = _resolve_task_file(task_id, "final_video.mp4")
    return FileResponse(
        str(target),
        media_type="video/mp4",
        filename="final_video.mp4",
        content_disposition_type="attachment",
    )


@router.get("/{task_id}/download-artifact")
def download_artifact(task_id: str, name: str = Query(..., description="相对任务目录的文件名")):
    """下载中间产物（脚本 / 报告 / 提示词 JSON、抽帧图片等）。"""
    target = _resolve_task_file(task_id, name)
    media_type = _MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(
        str(target),
        media_type=media_type,
        filename=target.name,
        content_disposition_type="attachment",
    )
