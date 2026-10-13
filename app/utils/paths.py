"""路径管理：项目根目录定位、运行时目录创建、安全路径校验。

约定（见 ARCHITECTURE §7.4）：
- 对外一律返回相对 ``outputs/`` 的相对路径（正斜杠），禁止暴露 ``E:\\`` 绝对路径；
- 所有文件名参数需经 :func:`Paths.safe_join` 做路径穿越校验。

冻结（PyInstaller）兼容：
- **可写**目录（``config/``、``outputs/``、``logs/``）必须落在 exe 同级目录，
  因此冻结时 :data:`PROJECT_ROOT` = ``os.path.dirname(sys.executable)``，
  **绝不能**写进 ``sys._MEIPASS`` 临时目录；
- **只读**资源（``app/static`` 等）由 PyInstaller 释放到 ``sys._MEIPASS``，
  故 :data:`RESOURCE_ROOT` 在冻结时指向 ``sys._MEIPASS``，非冻结时等于项目根。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path, PurePosixPath

__all__ = [
    "Paths",
    "PROJECT_ROOT",
    "RESOURCE_ROOT",
    "resolve_project_root",
    "resolve_resource_root",
]


def resolve_project_root() -> Path:
    """返回**可写**项目根目录。

    冻结（PyInstaller onedir / onefile）时返回 exe 所在目录，保证
    ``config/``、``outputs/``、``logs/`` 落在 exe 同级而非 ``sys._MEIPASS``
    临时目录；非冻结时返回仓库根目录。

    Returns:
        项目根目录（绝对路径）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # app/utils/paths.py -> parents[0]=utils, [1]=app, [2]=storyforge 项目根
    return Path(__file__).resolve().parents[2]


def resolve_resource_root() -> Path:
    """返回**只读**资源根目录（静态文件、模板等）。

    冻结时 PyInstaller 把 ``--add-data`` 资源释放到 ``sys._MEIPASS``；非冻结时
    资源即位于项目根目录。

    Returns:
        资源根目录（绝对路径）。
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return Path(meipass)
        return resolve_project_root()
    return resolve_project_root()


PROJECT_ROOT: Path = resolve_project_root()
RESOURCE_ROOT: Path = resolve_resource_root()


class Paths:
    """集中管理项目内的关键路径，并提供运行时目录的创建与安全拼接。"""

    ROOT: Path = PROJECT_ROOT
    RESOURCES: Path = RESOURCE_ROOT
    APP_DIR: Path = RESOURCE_ROOT / "app"
    DOCS_DIR: Path = PROJECT_ROOT / "docs"
    STATIC_DIR: Path = RESOURCE_ROOT / "app" / "static"

    CONFIG_DIR: Path = PROJECT_ROOT / "config"
    CONFIG_FILE: Path = CONFIG_DIR / "config.json"

    OUTPUTS_DIR: Path = PROJECT_ROOT / "outputs"
    LOGS_DIR: Path = PROJECT_ROOT / "logs"
    LOG_FILE: Path = LOGS_DIR / "app.log"
    PORT_FILE: Path = LOGS_DIR / "port.txt"

    # ------------------------------------------------------------------ #
    # 目录创建
    # ------------------------------------------------------------------ #
    @classmethod
    def ensure_runtime_dirs(cls) -> None:
        """创建运行时所需的配置 / 产物 / 日志目录（幂等）。"""
        for directory in (
            cls.CONFIG_DIR,
            cls.OUTPUTS_DIR,
            cls.LOGS_DIR,
            cls.STATIC_DIR,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def new_output_dir(cls) -> Path:
        """创建并返回本次任务独立的时间戳输出目录。

        目录命名为 ``outputs/YYYYMMDD_HHMMSS``（本地时间），并预建
        ``shots/`` 与 ``frames/`` 子目录；同名时追加序号避免冲突。

        Returns:
            输出目录的绝对路径。
        """
        cls.ensure_runtime_dirs()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = cls.OUTPUTS_DIR / stamp
        index = 1
        while candidate.exists():
            candidate = cls.OUTPUTS_DIR / f"{stamp}_{index}"
            index += 1
        (candidate / "shots").mkdir(parents=True, exist_ok=True)
        (candidate / "frames").mkdir(parents=True, exist_ok=True)
        return candidate

    # ------------------------------------------------------------------ #
    # 路径转换与安全校验
    # ------------------------------------------------------------------ #
    @classmethod
    def to_rel_outputs(cls, path: str | Path) -> str:
        """把绝对路径转换为相对 ``outputs/`` 的正斜杠相对路径。

        Args:
            path: 目标绝对路径（应位于 ``outputs/`` 之内）。

        Returns:
            形如 ``outputs/20260210_153000/script.json`` 的相对路径；
            若不在 outputs 内，则退化为文件名。
        """
        target = Path(path).resolve()
        try:
            relative = target.relative_to(cls.OUTPUTS_DIR.resolve())
            return str(PurePosixPath("outputs") / PurePosixPath(relative.as_posix()))
        except ValueError:
            return PurePosixPath(target.name).as_posix()

    @staticmethod
    def safe_join(base: str | Path, name: str) -> Path:
        """在 ``base`` 目录下安全拼接 ``name``，拒绝路径穿越。

        Args:
            base: 基准目录（如某任务输出目录）。
            name: 用户传入的相对文件名，允许形如 ``shots/shot_001.mp4``。

        Returns:
            解析后的绝对路径。

        Raises:
            ValueError: 参数为空、含空字节、为绝对路径或越出 base 目录。
        """
        if not name or not str(name).strip():
            raise ValueError("路径参数不能为空")
        raw = str(name)
        if "\x00" in raw:
            raise ValueError("路径参数非法（含空字节）")

        base_path = Path(base).resolve()
        candidate = (base_path / raw).resolve()

        # 允许 candidate == base（少见），其余必须位于 base 之内
        if candidate != base_path and base_path not in candidate.parents:
            raise ValueError("路径参数非法（检测到路径穿越）")
        return candidate
