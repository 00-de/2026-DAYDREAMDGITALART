"""文字モザイクモードの土台：入力文章 → 高解像度の白黒マスク画像。

マスク画像とは、文字の部分が白（255）、背景が黒（0）の画像のことです。
このマスクをタイルの升目に分割し、「白い升目＝文字の部分」に写真を優先配置します
（配置そのものはフェーズ6で実装）。

主な機能
- 文字数を「人が見て1文字」の単位（書記素）で数える（絵文字・濁点の合成文字も1文字）
- Windows のフォントフォルダーから日本語対応フォントを自動で探す
- 文字サイズを自動調整し、全体が必ずキャンバス内（余白の内側）に収まるようにする
- 自動折り返し・行のバランス調整・行頭禁則（「。」などが行頭に来ないように）
- 文字の太さ（縁取りで太らせる）、文字間隔、行間、1行／複数行の切り替え
- フォントに無い文字（□になる文字）を検出して警告
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import regex
from PIL import Image, ImageDraw, ImageFont

from . import config
from .errors import FontError, TextInputError

# 行頭に来てはいけない文字（行頭禁則）
_NO_LINE_START = set("、。，．・：；？！ー～）」』】〕〉》｝ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ,.!?:;)]}…‥")

_GRAPHEME = regex.compile(r"\X")


# --------------------------------------------------------------------------
# 文字数カウントと入力チェック
# --------------------------------------------------------------------------
def split_graphemes(text: str) -> list[str]:
    """文章を「人が見て1文字」の単位に分ける。"""
    return _GRAPHEME.findall(text)


def count_chars(text: str) -> int:
    """画面に表示する文字数。改行は数えない（スペースは数える）。"""
    return sum(1 for g in split_graphemes(text) if g not in ("\n", "\r\n", "\r"))


def normalize_text(text: str) -> str:
    """改行コードを統一し、前後の空白・空行を取り除く。"""
    t = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    lines = [ln.strip() for ln in t.split("\n")]
    # 先頭・末尾の空行を除去（途中の空行は「行を空ける」意図として残す）
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def validate_text(text: str) -> str:
    """生成前のチェック。問題なければ整えた文章を返し、ダメなら TextInputError。"""
    t = normalize_text(text)
    n = count_chars(t)
    if n < config.TEXT_MIN_CHARS or not t.strip():
        raise TextInputError(
            "文章が入力されていません。",
            f"{config.TEXT_MIN_CHARS}〜{config.TEXT_MAX_CHARS}文字の文章を入力してください。",
        )
    if n > config.TEXT_MAX_CHARS:
        raise TextInputError(
            f"文字数が多すぎます（{n}文字）。",
            f"{config.TEXT_MAX_CHARS}文字以内にしてください。",
        )
    return t


def truncate_to_limit(text: str, limit: int = config.TEXT_MAX_CHARS) -> str:
    """入力欄で上限を超えた分を切り落とす（41文字目以降を受け付けない処理用）。"""
    out, n = [], 0
    for g in split_graphemes(text):
        if g not in ("\n", "\r\n", "\r"):
            if n >= limit:
                break
            n += 1
        out.append(g)
    return "".join(out)


# --------------------------------------------------------------------------
# フォント探索
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FontInfo:
    display_name: str   # 画面に表示する名前（例：Meiryo Regular）
    path: str
    index: int = 0      # .ttc（複数フォント入り）の何番目か


def _font_dirs() -> list[Path]:
    dirs: list[Path] = []
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        dirs.append(Path(windir) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:  # ユーザーが個別にインストールしたフォント
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    else:  # 開発・テスト用（Linux / macOS）
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
                 Path.home() / ".fonts", Path("/System/Library/Fonts"), Path("/Library/Fonts")]
    return [d for d in dirs if d.is_dir()]


# よく使われる日本語フォントを一覧の先頭に出す（ファイル名の一部で判定）
_PREFERRED = ["meiryo", "yugoth", "biz-udgoth", "msgothic", "yumin", "uddigikyokasho",
              "notosanscjk", "notosansjp", "ipag", "ipaex"]


def _supports_japanese(font: ImageFont.FreeTypeFont) -> bool:
    try:
        return not any(_is_missing_glyph(font, ch) for ch in "あア漢")
    except Exception:
        return False


def _is_missing_glyph(font: ImageFont.FreeTypeFont, ch: str) -> bool:
    """その文字がフォントに無く「□（豆腐）」で表示されるかを判定する。"""
    if ch.isspace():
        return False
    notdef = _glyph_signature(font, "\U0010FFFD")  # 必ずフォントに無い文字
    return _glyph_signature(font, ch) == notdef


def _glyph_signature(font: ImageFont.FreeTypeFont, ch: str) -> bytes:
    m = font.getmask(ch)
    return repr(m.size).encode() + bytes(m)


@lru_cache(maxsize=1)
def find_japanese_fonts() -> tuple[FontInfo, ...]:
    """PC内の日本語対応フォントを探して一覧で返す（結果は記憶して2回目以降は即時）。"""
    found: dict[str, FontInfo] = {}
    for d in _font_dirs():
        for f in d.rglob("*"):
            if f.suffix.lower() not in (".ttf", ".ttc", ".otf"):
                continue
            max_index = 8 if f.suffix.lower() == ".ttc" else 1
            for idx in range(max_index):
                try:
                    font = ImageFont.truetype(str(f), 32, index=idx)
                except (OSError, ValueError):
                    break  # .ttc の中身が尽きた／読めないフォント
                if not _supports_japanese(font):
                    continue
                family, style = font.getname()
                name = f"{family} {style}".strip()
                found.setdefault(name, FontInfo(name, str(f), idx))

    def sort_key(fi: FontInfo):
        low = Path(fi.path).name.lower()
        rank = next((i for i, k in enumerate(_PREFERRED) if k in low), len(_PREFERRED))
        # 同じ系統なら日本向け（JP）を優先
        jp = 0 if (" JP" in fi.display_name or "J " in fi.display_name) else 1
        return (rank, jp, fi.display_name)

    return tuple(sorted(found.values(), key=sort_key))


def default_font() -> FontInfo:
    fonts = find_japanese_fonts()
    if not fonts:
        raise FontError(
            "日本語を表示できるフォントが見つかりません。",
            "Windows の「設定 → 個人用設定 → フォント」で日本語フォント（メイリオ・游ゴシック等）を確認してください。",
        )
    # 極太・極細は漢字がつぶれたり、写真で埋めにくいので初期値では避ける
    def style_rank(fi: FontInfo) -> int:
        n = fi.display_name.lower()
        if any(k in n for k in ("black", "heavy", "thin", "light", "extra", "ultra", "semi", "demi")):
            return 2
        return 0 if ("bold" in n or "regular" in n or "medium" in n) else 1
    for rank in (0, 1, 2):
        for fi in fonts:
            if style_rank(fi) == rank:
                return fi
    return fonts[0]


# --------------------------------------------------------------------------
# マスク生成
# --------------------------------------------------------------------------
@dataclass
class TextMaskSettings:
    font: FontInfo | None = None        # None なら自動で日本語フォントを選ぶ
    canvas_size: tuple[int, int] = (2400, 1600)  # マスク画像の大きさ（幅, 高さ）
    weight: int = 1                     # 文字の太さ 0（元の太さ）〜10（極太）
    letter_spacing: float = 0.05        # 文字間隔（文字サイズに対する割合）
    line_spacing: float = 1.15          # 行間（文字サイズに対する倍率）
    multiline: bool = True              # True：複数行（自動折り返し）／False：1行
    margin: float = 0.06                # 余白（キャンバスの短辺に対する割合）
    font_size: int | None = None        # None なら収まる最大サイズを自動計算


@dataclass
class TextMaskResult:
    mask: Image.Image                   # "L" モード。文字=255、背景=0
    text: str
    lines: list[str]
    font_size: int
    font: FontInfo
    text_box: tuple[int, int, int, int]  # 文字が描かれた範囲（左, 上, 右, 下）
    warnings: list[str] = field(default_factory=list)

    def as_array(self) -> np.ndarray:
        return np.asarray(self.mask, dtype=np.uint8)


def _stroke(size: int, weight: int) -> int:
    return max(0, round(size * max(0, min(10, weight)) * 0.012))


_NEWLINES = ("\n", "\r\n", "\r")
_WORD_CHAR = regex.compile(r"^[\p{Latin}\p{N}'’\-_&@#%+.]+$")


def _units(graphemes: list[str]) -> list[list[str]]:
    """折り返しの最小単位に分ける。
    英単語・数字のかたまり（例：DayDream, 2027）は途中で切らない。
    日本語は1文字ずつ。スペースと改行はそれぞれ単独の単位。
    """
    units: list[list[str]] = []
    for g in graphemes:
        is_word = bool(_WORD_CHAR.match(g))
        if is_word and units and units[-1] and bool(_WORD_CHAR.match(units[-1][-1])):
            units[-1].append(g)
        else:
            units.append([g])
    return units


class _Measurer:
    """ある文字サイズでの文字幅を測る係（同じ文字は計算結果を使い回す）。"""

    def __init__(self, font_info: FontInfo, size: int, weight: int, spacing: float):
        try:
            self.font = ImageFont.truetype(font_info.path, size, index=font_info.index)
        except OSError:
            raise FontError(f"フォントを開けません：{font_info.display_name}",
                            "別のフォントを選んでください。")
        self.size = size
        self.stroke = _stroke(size, weight)
        self.spacing = spacing
        self._cache: dict[str, float] = {}

    def adv(self, g: str) -> float:
        if g not in self._cache:
            self._cache[g] = self.font.getlength(g) + self.size * self.spacing + self.stroke * 2
        return self._cache[g]

    def width(self, gs) -> float:
        return sum(self.adv(g) for g in gs)


def _wrap(units: list[list[str]], m: _Measurer, max_width: float) -> list[list[str]]:
    """幅 max_width に収まるよう折り返す（単語は切らない・行頭禁則つき・改行は強制改行）。"""
    lines: list[list[str]] = [[]]
    width = 0.0
    for u in units:
        if u[0] in _NEWLINES:
            lines.append([])
            width = 0.0
            continue
        w = m.width(u)
        cur = lines[-1]
        if u == [" "]:
            if cur:  # 行頭の空白は捨てる
                cur.append(" ")
                width += w
            continue
        if cur and width + w > max_width and u[0] not in _NO_LINE_START:
            while cur and cur[-1] == " ":  # 行末の空白を除去
                cur.pop()
            if w > max_width:  # 1行に入りきらない長い単語は文字単位で分割
                lines.append([])
                cur, width = lines[-1], 0.0
            else:
                lines.append(list(u))
                width = w
                continue
        if w > max_width and not cur:
            for g in u:
                a = m.adv(g)
                if lines[-1] and width + a > max_width:
                    lines.append([])
                    width = 0.0
                lines[-1].append(g)
                width += a
            continue
        cur.extend(u)
        width += w
    for ln in lines:
        while ln and ln[-1] == " ":
            ln.pop()
    return lines


def _lines_for_count(units, m: _Measurer, n: int) -> list[list[str]] | None:
    """ちょうど n 行以内で、いちばん横幅が短くなる折り返し方を探す（行の長さがそろう）。"""
    total = m.width([g for u in units for g in u if g not in _NEWLINES])
    lo = max((m.width(u) for u in units if u[0] not in _NEWLINES), default=1.0)
    lo = min(lo, total)
    hi = total + 1
    if len(_wrap(units, m, hi)) > n:
        return None
    for _ in range(30):
        mid = (lo + hi) / 2
        if len(_wrap(units, m, mid)) <= n:
            hi = mid
        else:
            lo = mid
    return _wrap(units, m, hi)


def _render_lines(lines: list[list[str]], m: _Measurer, line_spacing: float):
    """行を中央揃えで描き、文字のない余白を切り取った画像を返す。"""
    font, size, stroke = m.font, m.size, m.stroke
    ascent, descent = font.getmetrics()
    widths = [m.width(ln) for ln in lines]
    max_w = max(widths, default=0)
    line_h = size * line_spacing
    pad = stroke * 2 + size // 2
    block_w = int(max_w) + pad * 2
    block_h = int(ascent + descent + line_h * max(0, len(lines) - 1)) + pad * 2
    img = Image.new("L", (max(1, block_w), max(1, block_h)), 0)
    draw = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        x = pad + (max_w - widths[i]) / 2
        baseline = pad + ascent + line_h * i
        for g in ln:
            if not g.isspace():
                draw.text((x + stroke, baseline), g, font=font, fill=255, anchor="ls",
                          stroke_width=stroke, stroke_fill=255)
            x += m.adv(g)
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else None


def render_text_mask(text: str, settings: TextMaskSettings | None = None) -> TextMaskResult:
    """文章から白黒マスク画像を作る。全体が余白の内側に必ず収まるよう文字サイズを決める。

    複数行モードでは「1行・2行・3行…」の候補を試し、文字が最も大きく読める行数を選ぶ。
    ただし行数が少ない方が読みやすいので、大きさの差が小さい（80%以上）なら少ない行数を優先。
    """
    s = settings or TextMaskSettings()
    t = validate_text(text)
    font_info = s.font or default_font()
    cw, ch = s.canvas_size
    if cw < 100 or ch < 100:
        raise TextInputError("キャンバスが小さすぎます。", "幅・高さを100ピクセル以上にしてください。")
    margin = int(min(cw, ch) * s.margin)
    avail_w, avail_h = cw - margin * 2, ch - margin * 2
    warnings: list[str] = []

    source = t if s.multiline else t.replace("\n", " ")
    graphemes = split_graphemes(source)
    units = _units(graphemes)

    def fits(img) -> bool:
        return img is not None and img.width <= avail_w and img.height <= avail_h

    def fit_size(lines: list[list[str]], start: int):
        """行の割り方を固定したまま、収まる最大の文字サイズを探す。"""
        size = max(8, start)
        img = _render_lines(lines, _Measurer(font_info, size, s.weight, s.letter_spacing), s.line_spacing)
        if fits(img):
            while True:  # 少しずつ大きくして限界を探す
                nxt = int(size * 1.03) + 1
                im2 = _render_lines(lines, _Measurer(font_info, nxt, s.weight, s.letter_spacing), s.line_spacing)
                if not fits(im2):
                    return size, img
                size, img = nxt, im2
        while size > 8:
            size = int(size * 0.97)
            img = _render_lines(lines, _Measurer(font_info, size, s.weight, s.letter_spacing), s.line_spacing)
            if fits(img):
                return size, img
        return (size, img) if fits(img) else (None, None)

    REF = 100  # 行の割り方を決めるための基準サイズ
    ref = _Measurer(font_info, REF, s.weight, s.letter_spacing)

    if s.font_size:
        size = s.font_size
        while True:
            m = _Measurer(font_info, size, s.weight, s.letter_spacing)
            lines = _wrap(units, m, avail_w) if s.multiline else [[g for g in graphemes]]
            img = _render_lines(lines, m, s.line_spacing)
            if fits(img) or size <= 8:
                break
            size = int(size * 0.94)
        if not fits(img):
            raise TextInputError("文章がキャンバスに収まりません。",
                                 "文字数を減らすか、キャンバスを大きくしてください。")
        if size != s.font_size:
            warnings.append(f"指定の文字サイズでは収まらないため、{size}px に縮小しました。")
    else:
        paragraphs = 1 + sum(1 for u in units if u[0] in _NEWLINES)
        max_lines = paragraphs if not s.multiline else min(8, max(paragraphs, len(units)))
        candidates = []
        seen = set()
        for n in range(paragraphs, max_lines + 1):
            lines = _lines_for_count(units, ref, n) if s.multiline else [list(graphemes)]
            if lines is None:
                continue
            key = tuple("".join(l) for l in lines)
            if key in seen:
                continue
            seen.add(key)
            ref_img = _render_lines(lines, ref, s.line_spacing)
            if ref_img is None:
                continue
            guess = int(REF * min(avail_w / ref_img.width, avail_h / ref_img.height))
            sz, img = fit_size(lines, guess)
            if sz:
                candidates.append((len(lines), sz, lines, img))
            if not s.multiline:
                break
        if not candidates:
            raise TextInputError("文章がキャンバスに収まりません。",
                                 "文字数を減らすか、キャンバスを大きくしてください。")
        best_size = max(c[1] for c in candidates)
        _, size, lines, img = min((c for c in candidates if c[1] >= best_size * 0.80),
                                  key=lambda c: c[0])

    if img is None:
        raise TextInputError("表示できる文字がありません。", "文章の内容を確認してください。")

    final_font = _Measurer(font_info, size, s.weight, s.letter_spacing).font
    missing = sorted({g for g in graphemes if g not in _NEWLINES and _is_missing_glyph(final_font, g)})
    if missing:
        warnings.append("選んだフォントでは表示できない文字があります（□になります）："
                        + " ".join(missing) + "　→ 別のフォントを選ぶか、文字を変えてください。")

    mask = Image.new("L", (cw, ch), 0)
    ox, oy = (cw - img.width) // 2, (ch - img.height) // 2
    mask.paste(img, (ox, oy))
    # 縁のなめらかさ（アンチエイリアス）を残さず、はっきり白黒にする
    mask = mask.point(lambda v: 255 if v >= 128 else 0)

    if size < min(cw, ch) * 0.04:
        warnings.append("文字がかなり小さくなっています。読みやすさのため、文字数を減らすか1行あたりの文字を減らしてください。")

    return TextMaskResult(mask, t, ["".join(ln) for ln in lines], size, font_info,
                          (ox, oy, ox + img.width, oy + img.height), warnings)


# --------------------------------------------------------------------------
# タイルの升目への変換（フェーズ6の配置処理で使う）
# --------------------------------------------------------------------------
def grid_size_for(canvas_size: tuple[int, int], tile_px: int) -> tuple[int, int]:
    """タイル1枚の大きさから、横・縦のタイル数を計算する。"""
    return max(1, round(canvas_size[0] / tile_px)), max(1, round(canvas_size[1] / tile_px))


# 漢字の画数が多い字でも読めるよう、1文字の高さに最低何枚のタイルを並べるか
MIN_TILES_PER_CHAR = 16


def recommend_tile_px(result: TextMaskResult, min_tiles_per_char: int = MIN_TILES_PER_CHAR) -> int:
    """文字が判読できるタイルの大きさ（マスク画像上のピクセル）の目安を返す。"""
    return max(2, result.font_size // min_tiles_per_char)


def mask_coverage(mask: Image.Image, cols: int, rows: int) -> np.ndarray:
    """各升目のうち文字が占める割合（0.0〜1.0）を返す。形は (rows, cols)。"""
    small = mask.resize((cols, rows), Image.Resampling.BOX)
    return np.asarray(small, dtype=np.float32) / 255.0


def grid_preview(mask: Image.Image, cols: int, rows: int, threshold: float = 0.5,
                 cell: int = 8, text_color=(255, 255, 255), bg_color=(20, 20, 30)) -> Image.Image:
    """タイル化したときの見え方を確認する簡易プレビュー（読みやすさチェック用）。"""
    cov = mask_coverage(mask, cols, rows) >= threshold
    arr = np.where(cov[..., None], np.array(text_color, np.uint8), np.array(bg_color, np.uint8))
    img = Image.fromarray(arr.astype(np.uint8), "RGB")
    return img.resize((cols * cell, rows * cell), Image.Resampling.NEAREST)
