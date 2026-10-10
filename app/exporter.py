"""完成画像の保存（フェーズ8）。

安全のための工夫
- 元の写真（メイン写真・タイル写真）と同じ場所・同じ名前には保存させない（上書き防止）
- 保存前にディスクの空き容量を確認する
- いったん一時ファイルに書いてから名前を付け替える
  → 保存の途中でアプリが閉じても、壊れた画像ファイルが残らない
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import config
from .errors import MosaicError


@dataclass
class SaveOptions:
    path: str
    fmt: str = "PNG"          # "PNG" または "JPEG"
    quality: int = config.DEFAULT_JPEG_QUALITY
    scale: float = 1.0        # 出力解像度（1.0 = 100%）
    save_settings: bool = False


@dataclass
class SaveResult:
    path: str
    width: int
    height: int
    bytes: int
    settings_path: str = ""


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def estimate_file_bytes(width: int, height: int, fmt: str, quality: int) -> int:
    if fmt == "PNG":
        return int(width * height * 3 * 0.75)
    return int(width * height * 3 * (0.08 + 0.25 * (quality / 100) ** 3))


def save_image(img: Image.Image, opt: SaveOptions, protected_paths: set[str] | None = None,
               settings: dict | None = None) -> SaveResult:
    fmt = opt.fmt.upper()
    if fmt not in ("PNG", "JPEG"):
        raise MosaicError(f"対応していない保存形式です：{opt.fmt}", "PNG か JPEG を選んでください。")
    target = Path(opt.path)
    allowed = (".png",) if fmt == "PNG" else (".jpg", ".jpeg", ".jfif")
    if target.suffix.lower() not in allowed:  # 形式に合った拡張子にそろえる
        target = target.with_suffix(allowed[0])

    if protected_paths and _norm(target) in {_norm(p) for p in protected_paths}:
        raise MosaicError("元の写真と同じ名前で保存しようとしています。",
                          "元の写真を守るため、別のファイル名か別のフォルダーを選んでください。")
    folder = target.parent
    if not folder.is_dir():
        raise MosaicError(f"保存先のフォルダーが見つかりません：{folder}", "保存先を選び直してください。")

    w = max(1, round(img.width * opt.scale))
    h = max(1, round(img.height * opt.scale))
    need = estimate_file_bytes(w, h, fmt, opt.quality)
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        free = None
    if free is not None and need * 1.2 > free:
        raise MosaicError(f"保存先の空き容量が足りません（必要 約{need / 1e6:,.0f}MB／空き {free / 1e6:,.0f}MB）。",
                          "不要なファイルを削除するか、別のドライブに保存してください。")

    out = img if (w, h) == img.size else img.resize((w, h), Image.Resampling.LANCZOS)
    fd, tmp = tempfile.mkstemp(prefix=".ddp_saving_", suffix=target.suffix, dir=folder)
    os.close(fd)
    try:
        if fmt == "PNG":
            out.save(tmp, "PNG", optimize=False, compress_level=6)
        else:
            out.save(tmp, "JPEG", quality=int(opt.quality), optimize=True, progressive=True, subsampling=0)
        os.replace(tmp, target)
    except PermissionError:
        _remove(tmp)
        raise MosaicError("保存先に書き込めません（アクセスが拒否されました）。",
                          "別のフォルダー（「ピクチャ」や「デスクトップ」など）を選んでください。")
    except OSError as e:
        _remove(tmp)
        if getattr(e, "errno", None) == 28:  # 空き容量不足
            raise MosaicError("ディスクの空き容量が足りません。", "不要なファイルを削除するか、別のドライブに保存してください。")
        raise MosaicError(f"保存できませんでした：{e}", "保存先や空き容量を確認してください。")

    settings_path = ""
    if opt.save_settings and settings is not None:
        sp = target.with_suffix(".json")
        if not (protected_paths and _norm(sp) in {_norm(p) for p in protected_paths}):
            data = {"app": config.APP_NAME, "image": target.name, "output_size": [w, h], **settings}
            sp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            settings_path = str(sp)

    return SaveResult(str(target), w, h, target.stat().st_size, settings_path)


def _remove(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass
