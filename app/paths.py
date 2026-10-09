"""ファイルの置き場所をまとめる。EXE化したときと、そのまま動かしたときの両方に対応。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import config


def resource_dir() -> Path:
    """アイコンなど、アプリに同梱したファイルの場所。"""
    base = getattr(sys, "_MEIPASS", None)  # EXE化したときは PyInstaller が教えてくれる
    return Path(base) if base else Path(__file__).resolve().parents[1]


def asset(name: str) -> Path:
    return resource_dir() / "assets" / name


def user_data_dir() -> Path:
    """解析結果のキャッシュなどを置く場所（元の写真フォルダーは汚さない）。"""
    if sys.platform.startswith("win"):
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = root / config.APP_ID
    d.mkdir(parents=True, exist_ok=True)
    return d
