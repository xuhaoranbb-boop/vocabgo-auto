# -*- coding: utf-8 -*-
"""路径解析：源码运行 vs PyInstaller 打包后运行，配置/题库都要落在「软件自己那一层」。

打包成 exe 后 `__file__` 指向临时解包目录（会被清掉），必须改用 exe 所在目录，
否则用户填的 token、积累的题库一关软件就没了。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否跑在 PyInstaller 打出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """软件目录：源码运行 = 项目根；打包后 = exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """只读资源目录：打包后是 PyInstaller 的解包目录 _MEIPASS。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return app_dir()


def config_path(override: str | os.PathLike[str] | None = None) -> Path:
    if override:
        path = Path(override)
        return path if path.is_absolute() else app_dir() / path
    env_path = os.environ.get("VOCABGO_CONFIG")
    if env_path:
        return Path(env_path)
    return app_dir() / "config.json"


def bank_path(override: str | os.PathLike[str] | None = None) -> Path:
    if override:
        path = Path(override)
        return path if path.is_absolute() else app_dir() / path
    return app_dir() / "bank.json"


def asset(relative: str) -> Path:
    """取打包进去的资源（图标等）。"""
    return resource_dir() / "assets" / relative
