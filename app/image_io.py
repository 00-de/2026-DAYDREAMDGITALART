"""画像を「安全に」読み込むための部品。

ポイント
- 拡張子ではなくファイルの中身で形式を判定（JFIF も中身は JPEG なので正しく読める）
- 破損ファイル・画像でないファイルは ImageLoadError にして、呼び出し側が処理を続けられる
- スマホ写真の EXIF 回転情報を反映して正しい向きにする
- 透過PNGは背景色と合成して RGB にそろえる
- 大きなJPEGは縮小読み込み（draft）で高速・省メモリに読む
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from PIL import Image, ImageOps, UnidentifiedImageError

from . import config
from .errors import ImageLoadError

# Pillow 自体の巨大画像ガードもアプリの上限に合わせる
Image.MAX_IMAGE_PIXELS = config.MAX_INPUT_PIXELS

# 拡張子から想定される形式（中身と食い違っていないかの確認用）
_EXPECTED_FORMAT_BY_EXT = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".jfif": "JPEG",
    ".png": "PNG",
}


@dataclass
class LoadedImage:
    image: Image.Image          # 常に RGB
    path: Path
    format: str                 # "JPEG" または "PNG"（中身から判定）
    original_size: tuple[int, int]  # 向き補正後の元サイズ（幅, 高さ）
    warning: str = ""           # 読めたが注意が必要なこと（拡張子違い等）


def _flatten_to_rgb(img: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    """透過やパレット形式などを、背景色と合成した RGB 画像に変換する。"""
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    )
    if has_alpha:
        rgba = img.convert("RGBA")
        base = Image.new("RGBA", rgba.size, background + (255,))
        base.alpha_composite(rgba)
        return base.convert("RGB")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def load_image(
    path: str | os.PathLike,
    max_side: int | None = None,
    alpha_background: tuple[int, int, int] = config.DEFAULT_ALPHA_BACKGROUND,
    data: bytes | None = None,
) -> LoadedImage:
    """画像を1枚読み込んで RGB で返す。

    data にファイルの中身（バイト列）を渡すと、ディスクを読み直さずにそれを使う
    （重複チェック用の指紋計算と画像の読み込みを、1回のファイル読み込みで済ませるため）。

    max_side を指定すると、長辺がその長さ以下になるよう縮小して返す
    （タイル解析やサムネイル用。メモリ節約になる）。
    失敗した場合は ImageLoadError を投げる。
    """
    p = Path(path)
    name = p.name

    def source():
        return io.BytesIO(data) if data is not None else p

    if data is None and not p.is_file():
        raise ImageLoadError(
            f"ファイルが見つかりません：{name}",
            "ファイルが移動・削除されていないか確認してください。",
        )

    try:
        # ① 中身の検査（verify はデータの整合性だけを調べ、画素は展開しない）
        with Image.open(source()) as probe:
            fmt = probe.format
            width, height = probe.size
            if fmt not in config.ACCEPTED_FORMATS:
                raise ImageLoadError(
                    f"対応していない画像形式です（{fmt}）：{name}",
                    "JPG・JPEG・JFIF・PNG の写真を使ってください。",
                )
            if width * height > config.MAX_INPUT_PIXELS:
                raise ImageLoadError(
                    f"画像が大きすぎます（{width}×{height}）：{name}",
                    "画像編集ソフトで縮小してから使ってください。",
                )
            probe.verify()

        # ② verify 後は同じオブジェクトが使えないため開き直して本読み込み
        with Image.open(source()) as img:
            # 縮小読み込みで画素数が変わる前に「元のサイズ」を記録（EXIFで縦横が入れ替わる場合も考慮）
            orientation = img.getexif().get(0x0112, 1)
            original_size = (height, width) if orientation in (5, 6, 7, 8) else (width, height)
            if max_side and fmt == "JPEG":
                # JPEG は 1/2・1/4・1/8 で高速に縮小デコードできる
                img.draft("RGB", (max_side, max_side))
            img.load()
            img = ImageOps.exif_transpose(img)  # スマホ写真の向きを補正
            rgb = _flatten_to_rgb(img, alpha_background)
            if max_side:
                rgb.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            rgb = rgb.copy()  # ファイルとの関連を切り離してから閉じる

    except ImageLoadError:
        raise
    except UnidentifiedImageError:
        raise ImageLoadError(
            f"画像ファイルとして認識できません：{name}",
            "画像以外のファイルか、壊れている可能性があります。このファイルは使われません。",
        )
    except Image.DecompressionBombError:
        raise ImageLoadError(
            f"画像が大きすぎるため安全のため読み込みを中止しました：{name}",
            "画像編集ソフトで縮小してから使ってください。",
        )
    except MemoryError:
        raise ImageLoadError(
            f"メモリが足りず読み込めませんでした：{name}",
            "他のアプリを閉じるか、画像を縮小してから再度お試しください。",
        )
    except (OSError, SyntaxError, ValueError, RuntimeError) as e:
        raise ImageLoadError(
            f"画像が壊れているため読み込めません：{name}",
            f"別の写真をお使いください。（詳細：{e}）",
        )

    warning = ""
    expected = _EXPECTED_FORMAT_BY_EXT.get(p.suffix.lower())
    if expected and expected != fmt:
        warning = f"拡張子（{p.suffix}）と中身（{fmt}）が一致していません。中身に合わせて読み込みました。"

    return LoadedImage(rgb, p, fmt, original_size, warning)


def iter_candidate_files(folder: str | os.PathLike, recursive: bool = False) -> Iterator[Path]:
    """フォルダー内の「写真かもしれない」ファイルを列挙する（中身の検査はしない）。

    - 拡張子の大文字・小文字は区別しない（.JPG もOK）
    - 同じファイルが二重に出てこないようにする（Windows は大文字小文字を区別しない）
    - 日本語のフォルダー名・ファイル名もそのまま扱える
    """
    root = Path(folder)
    pattern = root.rglob("*") if recursive else root.iterdir()
    seen: set[str] = set()
    for f in sorted(pattern, key=lambda x: str(x).lower()):
        if not f.is_file() or f.suffix.lower() not in config.CANDIDATE_EXTENSIONS:
            continue
        key = os.path.normcase(str(f.resolve()))
        if key in seen:
            continue
        seen.add(key)
        yield f
