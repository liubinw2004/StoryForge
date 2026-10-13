"""配置持久化：读写 ``config/config.json``，api_key 脱敏与回填。

- 磁盘存储为**明文**（本地单机应用，风险可接受）；
- 对外提供 :meth:`get_public` 返回脱敏副本；
- :meth:`save` 收到脱敏值时**保留原值**，避免误覆盖真实 key。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.models.enums import VideoProvider
from app.models.schemas import Config, mask_secret
from app.utils.logger import get_logger
from app.utils.paths import Paths

__all__ = ["ConfigStore"]

_log = get_logger("config")

#: 当前合法的视频提供方取值（源自枚举）。**凡已给出但不在此集合内的取值一律回退 ``mock``**，
#: 既覆盖历史上已下线的提供方，也覆盖任何拼写错误/垃圾值（含数字等非字符串类型）——避免
#: Pydantic 因未知枚举校验失败而重置**整份**配置（丢失 LLM/画幅等）。
_ALLOWED_VIDEO_PROVIDERS: frozenset[str] = frozenset(p.value for p in VideoProvider)

#: 宽松布尔解析：可接受的「真」/「假」字符串字面量（大小写与首尾空白不敏感）。
_TRUTHY_TOKENS: frozenset[str] = frozenset({"true", "1", "yes", "y", "on"})
_FALSY_TOKENS: frozenset[str] = frozenset({"false", "0", "no", "n", "off", ""})

#: 一致性增量新增的布尔开关字段（仅剩提示词锁定卡）。
_BOOL_FIELDS: tuple[str, ...] = ("consistency_lock",)

#: **付费开关**：无法解析其取值时一律**显式写回 ``False``**（成本安全兜底）。
#: 自建图像生成已整体移除，现无付费布尔开关。
_COST_BOOL_FIELDS: frozenset[str] = frozenset()

#: RunningHub 工作流 i2v 合法的实例类型（成本安全：非法值回退 ``default`` 24G）。
_ALLOWED_RH_INSTANCE_TYPES: frozenset[str] = frozenset({"default", "plus", "ultra"})

#: RunningHub 工作流 i2v 中要求为**非空字符串**的配置字段（非法/空值删键回落默认）。
_RH_STRING_FIELDS: tuple[str, ...] = (
    "rh_workflow_id",
    "rh_first_frame_node_id",
    "rh_first_frame_field_name",
    "rh_prompt_node_id",
    "rh_prompt_field_name",
    "rh_duration_node_id",
    "rh_duration_field_name",
    "rh_seed_node_id",
    "rh_seed_field_name",
)

#: ``rh_retain_seconds`` 允许范围（结果保留秒数）。
_RH_RETAIN_MIN: int = 10
_RH_RETAIN_MAX: int = 180


class ConfigStore:
    """配置存储（线程安全）。"""

    _SECRET_FIELDS: tuple[str, ...] = ("llm_api_key", "video_api_key")

    def __init__(self, path: str | Path | None = None) -> None:
        """初始化并从磁盘加载配置（文件不存在则写入默认配置）。

        Args:
            path: 配置文件路径，默认 ``config/config.json``。
        """
        self.path: Path = Path(path) if path else Paths.CONFIG_FILE
        self._lock = threading.RLock()
        self._raw: Config = self._load_from_disk()

    # ------------------------------------------------------------------ #
    # 磁盘读写
    # ------------------------------------------------------------------ #
    def _load_from_disk(self) -> Config:
        """从磁盘读取配置；损坏或缺失时回退为默认配置并重写。"""
        if not self.path.exists():
            cfg = Config()
            self._write(cfg)
            _log.info("未找到配置文件，已生成默认配置：%s", self.path)
            return cfg
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = self._migrate_removed_providers(data)
                self._drop_null_video_provider(data)
                data = self._sanitize_extended_fields(data)
            cfg = Config.model_validate(data)
            return cfg
        except Exception as exc:
            _log.warning("配置文件解析失败（%s），已重置为默认配置", exc)
            cfg = Config()
            self._write(cfg)
            return cfg

    @staticmethod
    def _migrate_removed_providers(data: dict) -> dict:
        """把 ``video_provider`` 归一化到当前合法枚举：**凡已给出但不合法**的取值一律回退 ``mock``。

        覆盖两类情形：
          1. 历史上已下线的提供方取值；
          2. 任何拼写错误/垃圾值，包括**非字符串**类型（如数字 ``123``）——若不归一化，
             Pydantic 会因未知枚举校验失败而**重置整份配置**（丢失 LLM/画幅等用户设置）。

        语义：**只动 ``video_provider`` 字段**，其它字段原样保留。
        键缺失、或显式 ``None``（JSON ``null``）时**不改写**（``None`` 分支由
        :meth:`_drop_null_video_provider` 在加载阶段按缺省处理）。

        Args:
            data: 从磁盘读出的原始配置字典。

        Returns:
            归一化后的字典（同一对象）。
        """
        if "video_provider" not in data:
            return data
        provider = data.get("video_provider")
        if provider is None:
            return data
        if isinstance(provider, str) and provider in _ALLOWED_VIDEO_PROVIDERS:
            return data
        _log.warning(
            "检测到非法的视频提供方 %r（合法值：%s），已自动回退为 mock（其它配置保持不变）",
            provider,
            "/".join(sorted(_ALLOWED_VIDEO_PROVIDERS)),
        )
        data["video_provider"] = "mock"
        return data

    @staticmethod
    def _drop_null_video_provider(data: dict) -> None:
        """把显式 ``null`` 的 ``video_provider`` 视同缺省：删除该键，令 Pydantic 应用默认 ``mock``。

        若不处理，``video_provider: null`` 会因枚举 ``input_value=None`` 校验失败而走
        「重置为默认配置」分支——**把用户的 LLM/画幅等设置一并清空**。删除该键即可让
        Pydantic 落到字段默认值（``mock``），其它字段原样保留。

        Args:
            data: 从磁盘读出的原始配置字典（原地修改）。
        """
        if data.get("video_provider", "mock") is None:
            _log.warning(
                "检测到空的视频提供方 null，已按缺省处理为 mock（其它配置保持不变）"
            )
            data.pop("video_provider", None)

    @staticmethod
    def _coerce_bool(value) -> bool | None:
        """把磁盘上的**宽松布尔表示**归一化为 ``bool``；无法解析时返回 ``None``。

        接受（按优先级）：

        - 真正的 ``bool`` → 原样返回；
        - ``int``/``float``（含 ``0``/``1``）→ 按 ``!= 0`` 判定（对齐 Python 真值语义）；
        - 字符串（**忽略大小写与首尾空白**）：
          ``"true"/"1"/"yes"/"y"/"on"`` → ``True``；
          ``"false"/"0"/"no"/"n"/"off"/""`` → ``False``。

        其它任何类型/取值（``None``、列表、字典、无法识别的字符串等）→ ``None``（即「无法解析」）。

        Args:
            value: 磁盘读出的原始取值。

        Returns:
            归一化后的 ``bool``；无法解析时 ``None``。
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            token = value.strip().lower()
            if token in _TRUTHY_TOKENS:
                return True
            if token in _FALSY_TOKENS:
                return False
        return None

    @staticmethod
    def _sanitize_extended_fields(data: dict) -> dict:
        """归一化「一致性锁定卡 + RunningHub 工作流 i2v」扩展字段，**绝不触发 Pydantic 整份重置**
        （延续 :meth:`_migrate_removed_providers` 的防脏化原则）。

        布尔组（:data:`_BOOL_FIELDS`，仅 ``consistency_lock``）：

        - 键不存在 → 不动（走字段默认值）；
        - 可被 :meth:`_coerce_bool` 解析（``true``/``1``/``"yes"``/``"off"`` …）→ **写回真正的
          ``bool``**（用户手写 ``0``/``"false"`` 能正确关掉开关）；
        - **无法解析** → 记 warn 并删除该键令其回落字段默认值（当前无付费布尔开关，
          :data:`_COST_BOOL_FIELDS` 为空）。

        RunningHub 字符串组（:data:`_RH_STRING_FIELDS`）：键存在且值**不是** ``str`` 或为
        **空串** → 删除该键并记 warn（回落 Pydantic 字段默认值，避免提交空 node/field 造成浪费）。

        RunningHub 实例类型（``rh_instance_type``）：键存在但**不是** ``str`` 或不在
        :data:`_ALLOWED_RH_INSTANCE_TYPES` 内 → **显式写回 ``"default"``**（成本安全红线：非法值
        一律回退 24G 默认实例）。

        RunningHub 保留秒数（``rh_retain_seconds``）：值**不是** ``int``（``bool`` 视同非法）→
        删除该键并记 warn；越界则**钳制**到 :data:`_RH_RETAIN_MIN`–:data:`_RH_RETAIN_MAX`
        （10–180）。

        **除目标键外，其它字段一律不动**（键缺失/类型合法时原样保留）。

        Args:
            data: 从磁盘读出的原始配置字典（原地修改）。

        Returns:
            归一化后的字典（同一对象）。
        """
        for key in _BOOL_FIELDS:
            if key not in data:
                continue
            coerced = ConfigStore._coerce_bool(data[key])
            if coerced is not None:
                data[key] = coerced
                continue
            _log.warning(
                "检测到无法解析的 %s=%r（应为布尔值），已按缺省处理（其它配置保持不变）",
                key,
                data[key],
            )
            if key in _COST_BOOL_FIELDS:
                data[key] = False
            else:
                del data[key]

        for key in _RH_STRING_FIELDS:
            if key in data and (not isinstance(data[key], str) or not data[key].strip()):
                _log.warning(
                    "检测到非法的 %s=%r（应为非空字符串），已按缺省处理（其它配置保持不变）",
                    key,
                    data[key],
                )
                del data[key]

        # rh_instance_type：非法值一律回退 default（成本安全红线）
        if "rh_instance_type" in data:
            value = data["rh_instance_type"]
            if not isinstance(value, str) or value not in _ALLOWED_RH_INSTANCE_TYPES:
                _log.warning(
                    "检测到非法的 rh_instance_type=%r（合法值：%s），已回退为 default",
                    value,
                    "/".join(sorted(_ALLOWED_RH_INSTANCE_TYPES)),
                )
                data["rh_instance_type"] = "default"

        # rh_retain_seconds：非 int（bool 视同非法）→ 删键；越界 → 钳制 10–180
        if "rh_retain_seconds" in data:
            value = data["rh_retain_seconds"]
            if isinstance(value, bool) or not isinstance(value, int):
                _log.warning(
                    "检测到非法的 rh_retain_seconds=%r（应为整数），"
                    "已按缺省处理（其它配置保持不变）",
                    value,
                )
                del data["rh_retain_seconds"]
            elif value < _RH_RETAIN_MIN or value > _RH_RETAIN_MAX:
                _log.warning(
                    "检测到越界的 rh_retain_seconds=%r（应为 10–180），已钳制",
                    value,
                )
                data["rh_retain_seconds"] = max(_RH_RETAIN_MIN, min(_RH_RETAIN_MAX, value))

        return data

    def _write(self, cfg: Config) -> None:
        """将配置以 UTF-8 明文写回磁盘。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False, indent=2)
        self.path.write_text(payload, encoding="utf-8")

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def get_raw(self) -> Config:
        """返回真实配置（含明文 api_key）的深拷贝。"""
        with self._lock:
            return self._raw.model_copy(deep=True)

    def get_public(self) -> Config:
        """返回 api_key 脱敏后的配置副本。"""
        with self._lock:
            return self._raw.to_public()

    def merge_secrets(self, incoming: Config) -> Config:
        """用磁盘上的真实 key 回填入参中的脱敏 key。

        Args:
            incoming: 来自前端/请求的配置（key 可能为脱敏值）。

        Returns:
            合并后的配置（脱敏值被替换为真实值）。
        """
        with self._lock:
            merged = incoming.model_copy(deep=True)
            for field in self._SECRET_FIELDS:
                raw_value = getattr(self._raw, field)
                current = getattr(merged, field)
                if raw_value and current == mask_secret(raw_value):
                    setattr(merged, field, raw_value)
            return merged

    def save(self, incoming: Config) -> Config:
        """保存配置（脱敏值不覆盖原值），并返回脱敏后的副本。

        Args:
            incoming: 待保存的配置。

        Returns:
            已落盘配置的脱敏副本。

        Raises:
            OSError: 写盘失败。
        """
        with self._lock:
            merged = self.merge_secrets(incoming)
            self._raw = merged
            self._write(merged)
            _log.info("配置已保存到 %s", self.path)
            return merged.to_public()
