"""RunningHub 工作流 i2v 视频适配器（图生视频，按次计费）。

RunningHub **工作流 API**（MiniMax H3 FL2VA 首帧参考生视频）采用「上传首帧 → 提交工作流 →
轮询 → 下载」的四步异步流程：

  1) ``POST {BASE}/openapi/v2/media/upload/binary``  上传首帧 → ``{"data": {"fileName": "..."}}``
  2) ``POST {BASE}/openapi/v2/run/workflow/{workflow_id}`` 提交 → ``{"taskId": "...", "status": "RUNNING"}``
  3) ``POST {BASE}/openapi/v2/query``               轮询 → ``{"status": "SUCCESS" | "FAILED", "results": [...]}``
  4) ``GET  results[0].url``                        下载 mp4 字节流并落盘到 ``out_path``

其中 ``BASE`` 默认 **国内站** ``https://www.runninghub.cn``（国际站 ``https://www.runninghub.ai``）。
鉴权：``Authorization: Bearer <API_KEY>``（``Content-Type: application/json``）。

**与旧「模型 API」的关键差异**（均已对齐官方工作流契约）：

- 上传必须取 ``data.fileName``（**不是** ``download_url``）：工作流 ``LoadImage`` 只认相对路径；
- 提交体为 ``nodeInfoList``（首帧图 / 提示词 / 时长 / 种子四类节点），字段名必须是
  ``fieldValue``（不是 ``value``）；
- image/prompt 节点传**字符串**，duration_seconds/seed 节点传**整数**；
- 提交端点 ``/openapi/v2/run/workflow/{rh_workflow_id}``，实例类型 ``rh_instance_type``、
  结果保留秒数 ``rh_retain_seconds`` 均由配置提供；
- 首帧**每段重新上传**（不缓存 ``fileName``，规避 24h 过期，简化实现）。

零新增第三方依赖：仅使用项目已装的 ``httpx``。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.models.enums import VideoProvider
from app.services.adapters.base import VideoError, VideoGeneratorInterface
from app.utils.logger import get_logger
from app.utils.responses import ErrorCode

__all__ = ["RunningHubAdapter"]

_log = get_logger("video")

#: 默认站点（国内站）。国际站为 https://www.runninghub.ai。
DEFAULT_BASE_URL = "https://www.runninghub.cn"
#: 任务查询端点路径（提交与查询均为 POST）。
QUERY_PATH = "/openapi/v2/query"
#: 媒体上传端点路径（multipart/form-data，字段名 ``file``）。
UPLOAD_PATH = "/openapi/v2/media/upload/binary"
#: 工作流提交端点模板（``{workflow_id}`` 由配置 ``rh_workflow_id`` 填充）。
WORKFLOW_RUN_PATH_TEMPLATE = "/openapi/v2/run/workflow/{workflow_id}"

# 轮询与 HTTP 超时（模块级常量，便于单测 patch）
_POLL_INTERVAL_SEC = 5.0
_POLL_TIMEOUT_SEC = 600.0
_HTTP_TIMEOUT_SEC = 60.0

# 视为「无错误」的 errorCode 取值（空串 / "0"）——其余非空值判为业务失败。
_NO_ERROR_CODES = frozenset({"", "0"})

# 疑似鉴权失败的判据关键词（在 errorCode/errorMessage 文本中匹配，大小写不敏感）。
_AUTH_HINTS: tuple[str, ...] = ("key", "401", "403", "433")


class RunningHubAdapter(VideoGeneratorInterface):
    """RunningHub 工作流 i2v 视频适配器（真实实现）。"""

    name = VideoProvider.RUNNINGHUB

    def __init__(self, cfg) -> None:
        """初始化，解析 api_key / base_url 与工作流节点参数。

        解析后 ``self.base_url`` 恒为 ``scheme://netloc``（供 ``QUERY_PATH`` /
        ``WORKFLOW_RUN_PATH_TEMPLATE`` 拼接）。工作流节点参数全部来自 ``rh_*`` 配置，
        缺省即回退官方实测值（首帧 ``4/image``、提示词 ``7/prompt``、时长
        ``6/duration_seconds``、种子 ``9/seed``）。

        Args:
            cfg: 全局配置。
        """
        super().__init__(cfg)
        self.api_key = cfg.video_api_key or ""

        raw_base = (cfg.video_base_url or "").strip() or DEFAULT_BASE_URL
        self.base_url, _ = self._split_url(raw_base)

        self.workflow_id = str(getattr(cfg, "rh_workflow_id", "") or "").strip()
        self.instance_type = str(getattr(cfg, "rh_instance_type", "default") or "default").strip()
        self.retain_seconds = int(getattr(cfg, "rh_retain_seconds", 180) or 180)

        # 首帧图节点
        self.first_frame_node_id = str(getattr(cfg, "rh_first_frame_node_id", "4") or "4").strip()
        self.first_frame_field_name = str(
            getattr(cfg, "rh_first_frame_field_name", "image") or "image"
        ).strip()
        # 提示词节点
        self.prompt_node_id = str(getattr(cfg, "rh_prompt_node_id", "7") or "7").strip()
        self.prompt_field_name = str(
            getattr(cfg, "rh_prompt_field_name", "prompt") or "prompt"
        ).strip()
        # 时长节点
        self.duration_node_id = str(getattr(cfg, "rh_duration_node_id", "6") or "6").strip()
        self.duration_field_name = str(
            getattr(cfg, "rh_duration_field_name", "duration_seconds") or "duration_seconds"
        ).strip()
        # 种子节点
        self.seed_node_id = str(getattr(cfg, "rh_seed_node_id", "9") or "9").strip()
        self.seed_field_name = str(getattr(cfg, "rh_seed_field_name", "seed") or "seed").strip()

    @staticmethod
    def _split_url(raw: str) -> tuple[str, str]:
        """把 URL / 裸主机拆成 ``(scheme://netloc, path)``。

        - 缺少 scheme 时补 ``https://``（允许用户只填 ``www.runninghub.cn``）；
        - ``path`` 已去掉首尾 ``/`` 之外的规范化（保留前导 ``/``），根路径返回空串。

        Args:
            raw: 原始 URL 或裸主机名。

        Returns:
            ``(host, path)``；``raw`` 为空时返回 ``(DEFAULT_BASE_URL, "")``。
        """
        value = (raw or "").strip()
        if not value:
            return DEFAULT_BASE_URL, ""
        if "://" not in value:
            value = f"https://{value}"
        parts = urlsplit(value)
        host = f"{parts.scheme}://{parts.netloc}"
        path = parts.path.rstrip("/")
        return host, path

    # ------------------------------------------------------------------ #
    # 公共接口
    # ------------------------------------------------------------------ #
    def generate(
        self,
        prompt: str,
        image_ref: str | None,
        duration: int,
        size: str,
        style: str,
        out_path: str,
    ) -> str:
        """旧「生成单个镜头」入口：工作流 i2v 专用，等价于带随机种子的 :meth:`generate_i2v`。

        工作流 i2v **必须有首帧图**；``image_ref`` 为空时抛出 ``VID_INVALID_PARAM``。
        ``style`` 参数忽略（风格由提示词承载）。

        Args:
            prompt: 提示词。
            image_ref: 首帧图本地路径（必填）。
            duration: 时长（秒）。
            size: 尺寸字符串（校验用，工作流画幅由节点决定）。
            style: 风格（忽略）。
            out_path: 输出 mp4 绝对路径。

        Returns:
            落盘后的绝对路径。

        Raises:
            VideoError: 参数非法 / 未配置 / 接口错误 / 生成失败。
        """
        if not image_ref:
            raise VideoError(
                ErrorCode.VID_INVALID_PARAM,
                "RunningHub 工作流 i2v 需要首帧图（image_ref 不能为空）",
            )
        import random

        seed = random.randint(0, 2_147_483_647)
        return self.generate_i2v(str(image_ref), prompt, duration, seed, size, out_path)

    def generate_i2v(
        self,
        first_frame: str,
        prompt: str,
        duration: int,
        seed: int,
        size: str,
        out_path: str,
    ) -> str:
        """走完「上传首帧 → 提交工作流 → 轮询 → 下载」并落盘，返回 mp4 绝对路径。

        Args:
            first_frame: 首帧图本地路径（必填）。
            prompt: 提示词。
            duration: 时长（秒，正整数）。
            seed: 随机种子（整数）。
            size: 尺寸字符串（仅做参数校验，不参与工作流请求体）。
            out_path: 输出 mp4 绝对路径。

        Returns:
            落盘后的绝对路径。

        Raises:
            VideoError: 参数非法（``VID_INVALID_PARAM``）、未配置（``VID_NOT_CONFIGURED``）、
                接口错误（``VID_API_ERROR``）或生成失败/超时（``VID_GENERATE_FAILED``）。
        """
        self._validate_params(duration, size, out_path)
        self._require_configured()

        file_name = self.upload_first_frame(str(first_frame))
        node_info_list = self.build_node_info_list(prompt, duration, seed, file_name)
        task_id = self.submit_workflow(node_info_list, self.instance_type, self.retain_seconds)
        return self._download(self._poll(task_id), out_path)

    def test_connection(self) -> dict:
        """零成本探针：**绝不创建真实生成任务**（避免花钱）。

        有 Key 时以 ``POST {BASE}{QUERY_PATH}`` + ``{"taskId": "0"}`` 探测端点与鉴权；
        无 Key 直接判负。其余结构化响应视为「可达」。

        Returns:
            ``{"ok": bool, "provider": str, "message": str}``
        """
        if not self.api_key:
            return {
                "ok": False,
                "provider": self.name.value,
                "message": "未配置 RunningHub API Key，请在「视频生成」配置区填写 API Key 后重试",
            }

        url = f"{self.base_url}{QUERY_PATH}"
        try:
            response = httpx.post(
                url, json={"taskId": "0"}, headers=self._headers(), timeout=_HTTP_TIMEOUT_SEC
            )
        except httpx.TimeoutException as exc:
            return {
                "ok": False,
                "provider": self.name.value,
                "message": f"无法连接 RunningHub（请求 {url} 超时）：{exc}",
            }
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "provider": self.name.value,
                "message": f"无法连接 RunningHub（{url}）：{exc}",
            }

        if response.status_code >= 400:
            return {
                "ok": False,
                "provider": self.name.value,
                "message": (
                    f"RunningHub 端点返回 HTTP {response.status_code}，请检查 Base URL 是否正确"
                    f"（{url}）"
                ),
            }

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "provider": self.name.value,
                "message": (
                    f"RunningHub 返回内容无法解析为 JSON（HTTP {response.status_code}）：{exc}"
                ),
            }
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "provider": self.name.value,
                "message": f"RunningHub 返回内容格式异常（HTTP {response.status_code}）",
            }

        code, message = self._extract_error(payload)
        if self._looks_like_auth_error(code, message):
            return {
                "ok": False,
                "provider": self.name.value,
                "message": (
                    "RunningHub API Key 无效或未授权"
                    f"（errorCode={code or '空'}，errorMessage：{message or '空'}）"
                ),
            }
        return {
            "ok": True,
            "provider": self.name.value,
            "message": (
                f"RunningHub 工作流端点可达、鉴权通过（HTTP {response.status_code}，"
                f"errorCode={code or '空'}）"
            ),
        }

    # ------------------------------------------------------------------ #
    # 工作流四步
    # ------------------------------------------------------------------ #
    def upload_first_frame(self, local_path: str) -> str:
        """第 1 步：把本地首帧图上传为工作流可引用的 ``fileName``。

        与旧模型 API 不同：**必须取 ``data.fileName``**（相对路径），缺失/为空即报错
        ``VID_API_ERROR``（工作流 ``LoadImage`` 只认该字段）。

        Args:
            local_path: 本地图片绝对路径。

        Returns:
            上传返回的 ``data.fileName``（非 URL）。

        Raises:
            VideoError: 文件不存在（``VID_INVALID_PARAM``）、未配置（``VID_NOT_CONFIGURED``）
                或上传/解析失败（``VID_API_ERROR``）。
        """
        self._require_configured()
        abs_path = os.path.abspath(local_path)
        if not os.path.isfile(abs_path):
            raise VideoError(ErrorCode.VID_INVALID_PARAM, f"首帧文件不存在：{local_path}")

        url = f"{self.base_url}{UPLOAD_PATH}"
        try:
            with open(abs_path, "rb") as fh:
                response = httpx.post(
                    url,
                    files={"file": (os.path.basename(abs_path), fh)},
                    headers=self._upload_headers(),
                    timeout=_HTTP_TIMEOUT_SEC,
                )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise VideoError(ErrorCode.VID_API_ERROR, f"上传首帧超时（{url}）：{exc}") from exc
        except httpx.HTTPError as exc:
            raise VideoError(ErrorCode.VID_API_ERROR, f"上传首帧失败（{url}）：{exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 非 JSON 响应等
            raise VideoError(ErrorCode.VID_API_ERROR, f"上传首帧响应解析失败：{exc}") from exc

        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else None
            raise VideoError(
                ErrorCode.VID_API_ERROR,
                f"上传首帧失败（服务端返回 code={code!r}）",
            )

        data = payload.get("data")
        file_name = ""
        if isinstance(data, dict):
            file_name = str(data.get("fileName") or "").strip()
        if not file_name:
            raise VideoError(
                ErrorCode.VID_API_ERROR,
                "上传首帧未返回 fileName（工作流 LoadImage 依赖该字段）",
            )
        _log.info("RunningHub 首帧已上传：%s → fileName=%s", os.path.basename(abs_path), file_name)
        return file_name

    def build_node_info_list(
        self,
        prompt: str,
        duration: int,
        seed: int,
        file_name: str,
    ) -> list[dict]:
        """组装工作流提交的 ``nodeInfoList``（四类节点）。

        契约（对齐官方 §5.3）：字段名必须是 ``fieldValue``（**不是** ``value``）；
        image/prompt 节点传字符串，duration_seconds/seed 节点传整数。

        Args:
            prompt: 提示词。
            duration: 时长（秒）。
            seed: 随机种子。
            file_name: 首帧图上传返回的 ``fileName``。

        Returns:
            按「首帧 → 提示词 → 时长 → 种子」顺序排列的节点列表。
        """
        return [
            {
                "nodeId": self.first_frame_node_id,
                "fieldName": self.first_frame_field_name,
                "fieldValue": file_name,
            },
            {
                "nodeId": self.prompt_node_id,
                "fieldName": self.prompt_field_name,
                "fieldValue": prompt or "",
            },
            {
                "nodeId": self.duration_node_id,
                "fieldName": self.duration_field_name,
                "fieldValue": int(duration),
            },
            {
                "nodeId": self.seed_node_id,
                "fieldName": self.seed_field_name,
                "fieldValue": int(seed),
            },
        ]

    def submit_workflow(
        self,
        node_info_list: list[dict],
        instance_type: str,
        retain_seconds: int,
    ) -> str:
        """第 2 步：提交工作流任务，返回 ``taskId``。

        Args:
            node_info_list: 节点参数列表（:meth:`build_node_info_list` 产出）。
            instance_type: 实例类型（``default`` / ``plus`` / ``ultra``）。
            retain_seconds: 结果保留秒数（10–180）。

        Returns:
            ``taskId``。

        Raises:
            VideoError: 未配置 API Key（``VID_NOT_CONFIGURED``）、未配置工作流 ID
                （``VID_NOT_CONFIGURED``）、接口错误（``VID_API_ERROR``）或缺失
                ``taskId``（``VID_API_ERROR``）。
        """
        self._require_configured()
        if not self.workflow_id:
            raise VideoError(
                ErrorCode.VID_NOT_CONFIGURED,
                "未配置 RunningHub 工作流 ID（rh_workflow_id），请在配置区填写后重试",
            )
        url = f"{self.base_url}{WORKFLOW_RUN_PATH_TEMPLATE.format(workflow_id=self.workflow_id)}"
        body = {
            "nodeInfoList": node_info_list,
            "instanceType": instance_type or "default",
            "usePersonalQueue": False,
            "retainSeconds": int(retain_seconds),
        }
        payload = self._post_json(url, body)
        code, message = self._extract_error(payload)
        if code not in _NO_ERROR_CODES:
            raise VideoError(
                ErrorCode.VID_API_ERROR,
                f"RunningHub 工作流提交失败（errorCode={code}，errorMessage：{message or '空'}）",
            )
        task_id = str(payload.get("taskId") or "").strip()
        if not task_id:
            raise VideoError(ErrorCode.VID_API_ERROR, "RunningHub 工作流未返回 taskId")
        _log.info(
            "RunningHub 工作流任务已提交：taskId=%s（workflow=%s instance=%s retain=%ss）",
            task_id,
            self.workflow_id,
            instance_type or "default",
            int(retain_seconds),
        )
        return task_id

    def _poll(self, task_id: str) -> str:
        """第 3 步：轮询任务状态，成功后返回下载地址 ``results[0].url``。

        状态流转：``QUEUED → RUNNING → SUCCESS | FAILED``；非空 ``errorCode`` 亦判为业务失败。
        """
        url = f"{self.base_url}{QUERY_PATH}"
        deadline = time.monotonic() + _POLL_TIMEOUT_SEC
        while True:
            payload = self._post_json(url, {"taskId": task_id})
            status = self._task_status(payload)
            normalized = status.strip().upper()
            code, message = self._extract_error(payload)

            if normalized == "SUCCESS":
                download_url = self._results_url(payload)
                if download_url:
                    return download_url
                raise VideoError(
                    ErrorCode.VID_GENERATE_FAILED,
                    "RunningHub 任务已成功但未返回下载地址（results[0].url 为空）",
                )
            if normalized == "FAILED":
                raise VideoError(
                    ErrorCode.VID_GENERATE_FAILED,
                    f"RunningHub 生成失败（status={status}，errorCode={code or '空'}，"
                    f"errorMessage：{message or '空'}）",
                )
            if code not in _NO_ERROR_CODES:
                raise VideoError(
                    ErrorCode.VID_GENERATE_FAILED,
                    f"RunningHub 生成失败（status={status or '未知'}，errorCode={code}，"
                    f"errorMessage：{message or '空'}）",
                )
            if time.monotonic() >= deadline:
                raise VideoError(
                    ErrorCode.VID_GENERATE_FAILED,
                    f"RunningHub 生成超时（已轮询约 {int(_POLL_TIMEOUT_SEC)}s，"
                    f"最后状态：{status or '未知'}）",
                )
            time.sleep(_POLL_INTERVAL_SEC)

    def _download(self, download_url: str, out_path: str) -> str:
        """第 4 步：下载视频字节流并落盘（mp4/H.264）。

        下载地址为 **COS 临时链接（约 24 小时过期）**，故拿到后立即下载。
        """
        out = Path(out_path)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VideoError(
                ErrorCode.VID_GENERATE_FAILED,
                f"无法创建输出目录（{out.parent}）：{exc}",
            ) from exc
        try:
            with httpx.stream("GET", download_url, timeout=_HTTP_TIMEOUT_SEC) as response:
                response.raise_for_status()
                with open(out, "wb") as handle:
                    for chunk in response.iter_bytes():
                        if chunk:
                            handle.write(chunk)
        except httpx.TimeoutException as exc:
            raise VideoError(
                ErrorCode.VID_GENERATE_FAILED, f"下载 RunningHub 视频超时：{exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise VideoError(
                ErrorCode.VID_GENERATE_FAILED, f"下载 RunningHub 视频失败：{exc}"
            ) from exc
        except OSError as exc:
            raise VideoError(
                ErrorCode.VID_GENERATE_FAILED, f"写入视频文件失败（{out_path}）：{exc}"
            ) from exc

        if not out.exists() or out.stat().st_size == 0:
            raise VideoError(ErrorCode.VID_GENERATE_FAILED, "RunningHub 视频下载结果为空")
        _log.info("RunningHub 视频已下载：%s（%d 字节）", out.name, out.stat().st_size)
        return str(out)

    # ------------------------------------------------------------------ #
    # HTTP 工具
    # ------------------------------------------------------------------ #
    def _headers(self) -> dict:
        """构造 JSON 请求头（Bearer 鉴权）。"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _upload_headers(self) -> dict:
        """媒体上传请求头（**仅** Bearer 鉴权；``Content-Type`` 交给 httpx 处理 multipart）。"""
        return {"Authorization": f"Bearer {self.api_key}"}

    def _post_json(self, url: str, body: dict) -> dict:
        """POST JSON 并解析结构化响应。"""
        try:
            response = httpx.post(
                url, json=body, headers=self._headers(), timeout=_HTTP_TIMEOUT_SEC
            )
        except httpx.TimeoutException as exc:
            raise VideoError(ErrorCode.VID_API_ERROR, f"请求 {url} 超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise VideoError(ErrorCode.VID_API_ERROR, f"无法连接 {url}：{exc}") from exc
        return self._decode(response, url)

    @staticmethod
    def _decode(response: httpx.Response, url: str) -> dict:
        """把 HTTP 响应解码为 dict；非 2xx / 非 JSON 一律抛 ``VID_API_ERROR``。"""
        if response.status_code >= 400:
            snippet = (response.text or "")[:200]
            raise VideoError(
                ErrorCode.VID_API_ERROR,
                f"RunningHub 返回错误状态码 HTTP {response.status_code}（{url}）：{snippet}",
            )
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise VideoError(
                ErrorCode.VID_API_ERROR,
                f"RunningHub 返回内容无法解析为 JSON（{url}）：{exc}",
            ) from exc
        if not isinstance(payload, dict):
            raise VideoError(
                ErrorCode.VID_API_ERROR, f"RunningHub 返回内容格式异常（{url}）"
            )
        return payload

    # ------------------------------------------------------------------ #
    # 响应解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_error(payload: dict) -> tuple[str, str]:
        """取出 ``(errorCode, errorMessage)``（缺失/非法时为空串）。"""
        code = payload.get("errorCode")
        message = payload.get("errorMessage")
        return str(code if code is not None else "").strip(), str(message or "").strip()

    @staticmethod
    def _task_status(payload: dict) -> str:
        """读取任务状态 ``status``。"""
        return str(payload.get("status") or "")

    @staticmethod
    def _results_url(payload: dict) -> str:
        """读取成功后的下载地址 ``results[0].url``（兼容元素为字符串的情形）。"""
        results = payload.get("results")
        if not isinstance(results, (list, tuple)) or not results:
            return ""
        first = results[0]
        if isinstance(first, dict):
            return str(first.get("url") or "").strip()
        if isinstance(first, str):
            return first.strip()
        return ""

    @staticmethod
    def _looks_like_auth_error(code: str, message: str) -> bool:
        """基于 ``errorCode`` / ``errorMessage`` 文本判断是否疑似鉴权失败。"""
        haystack = f"{code} {message}".lower()
        return any(hint in haystack for hint in _AUTH_HINTS)

    def _require_configured(self) -> None:
        """校验必要配置，缺失时抛出明确中文错误。"""
        if not self.api_key:
            raise VideoError(
                ErrorCode.VID_NOT_CONFIGURED,
                "未配置 RunningHub API Key，请在配置区填写 video_api_key 后重试",
            )
