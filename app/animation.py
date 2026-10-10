"""集合アニメーション：たくさんの写真が飛んできて、入力した文字ができあがる動画を作る。

■ 200種類のしくみ
  出発位置（10種類）× 集まる順番（5種類）× 動き方（4種類）＝ 200種類。
  さらに種類ごとに「回転」「拡大・縮小」「ふわっと現れる」「動きのなめらかさ」を変えて、
  同じ組み合わせでも表情が出るようにしている（種類の番号が同じなら、毎回同じ動きになる）。

■ 最後の画面は、文字モザイクの静止画とまったく同じ配置になる
  （静止画と同じ build_text_layout を使っているため）。

画面とは独立した部品。フレーム（1コマ）は QImage に QPainter で描く
（QImage への描画は作業係のスレッドでも安全に行える）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from . import mosaic_engine, text_mask
from .collection import PhotoRecord
from .errors import MosaicError

# --------------------------------------------------------------------------
# 200種類の定義
# --------------------------------------------------------------------------
ORIGINS = [
    ("scatter", "あちこちから"),
    ("left", "左から"),
    ("right", "右から"),
    ("top", "上から降ってくる"),
    ("bottom", "下からわき上がる"),
    ("center", "中心からはじける"),
    ("corners", "四隅から"),
    ("ring", "大きな輪から"),
    ("spiral", "うずまきから"),
    ("zoom", "手前から飛び込む"),
]
ORDERS = [
    ("together", "いっせいに"),
    ("ltr", "左から順に"),
    ("ttb", "上から順に"),
    ("center_out", "中心から外へ"),
    ("random", "ばらばらに"),
]
PATHS = [
    ("straight", "まっすぐ"),
    ("arc", "カーブを描いて"),
    ("swirl", "くるくる回りながら"),
    ("bounce", "はずんで"),
]


@dataclass(frozen=True)
class Preset:
    index: int          # 0〜199
    origin: str
    order: str
    path: str
    name: str
    rotation: float     # 出発時の回転（度）
    start_scale: float  # 出発時の大きさ（1＝そのまま）
    fade: bool          # ふわっと現れるか
    ease_power: float   # 動きのなめらかさ（大きいほど最後にゆっくり）


def _build_presets() -> list[Preset]:
    out = []
    i = 0
    for oi, (ok, oname) in enumerate(ORIGINS):
        for di, (dk, dname) in enumerate(ORDERS):
            for pi, (pk, pname) in enumerate(PATHS):
                rng = np.random.default_rng(10_000 + i)
                rot = 0.0
                if pk == "swirl":
                    rot = float(rng.choice([360, 540, 720]))
                elif rng.random() < 0.45:
                    rot = float(rng.choice([-180, -90, 90, 180, 270]))
                if ok == "zoom":
                    scale = float(rng.uniform(3.0, 6.0))
                elif ok == "center":
                    scale = 0.1
                else:
                    scale = float(rng.choice([1.0, 1.0, 0.3, 1.8]))
                fade = ok in ("zoom", "center") or bool(rng.random() < 0.35)
                power = float(rng.choice([2.0, 3.0, 4.0]))
                name = f"{i + 1:03d}　{oname}・{dname}・{pname}"
                out.append(Preset(i, ok, dk, pk, name, rot, scale, fade, power))
                i += 1
    return out


PRESETS: list[Preset] = _build_presets()
assert len(PRESETS) == 200


# --------------------------------------------------------------------------
# 動きの計算（numpy でまとめて計算）
# --------------------------------------------------------------------------
def _ease_out(p: np.ndarray, power: float) -> np.ndarray:
    return 1 - (1 - p) ** power


def _ease_out_back(p: np.ndarray) -> np.ndarray:
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (p - 1) ** 3 + c1 * (p - 1) ** 2


def _start_positions(origin: str, tgt: np.ndarray, W: float, H: float, rng) -> np.ndarray:
    n = len(tgt)
    cx, cy = W / 2, H / 2
    diag = math.hypot(W, H)
    if origin == "scatter":
        ang = rng.uniform(0, 2 * math.pi, n)
        rad = diag * rng.uniform(0.6, 0.9, n)
        return np.stack([cx + np.cos(ang) * rad, cy + np.sin(ang) * rad], 1)
    if origin == "left":
        return np.stack([-rng.uniform(0.05, 0.6, n) * W, tgt[:, 1] + rng.normal(0, H * 0.08, n)], 1)
    if origin == "right":
        return np.stack([W + rng.uniform(0.05, 0.6, n) * W, tgt[:, 1] + rng.normal(0, H * 0.08, n)], 1)
    if origin == "top":
        return np.stack([tgt[:, 0] + rng.normal(0, W * 0.05, n), -rng.uniform(0.05, 0.9, n) * H], 1)
    if origin == "bottom":
        return np.stack([tgt[:, 0] + rng.normal(0, W * 0.05, n), H + rng.uniform(0.05, 0.9, n) * H], 1)
    if origin == "center":
        return np.stack([cx + rng.normal(0, 4, n), cy + rng.normal(0, 4, n)], 1)
    if origin == "corners":
        corners = np.array([[-W * 0.1, -H * 0.1], [W * 1.1, -H * 0.1], [-W * 0.1, H * 1.1], [W * 1.1, H * 1.1]])
        d = ((tgt[:, None, :] - corners[None, :, :]) ** 2).sum(-1)
        return corners[d.argmin(1)] + rng.normal(0, diag * 0.02, (n, 2))
    if origin == "ring":
        ang = np.arctan2(tgt[:, 1] - cy, tgt[:, 0] - cx)
        return np.stack([cx + np.cos(ang) * diag * 0.75, cy + np.sin(ang) * diag * 0.75], 1)
    if origin == "spiral":
        k = rng.permutation(n) / max(1, n - 1)
        ang = k * 6 * math.pi
        rad = diag * (0.55 + 0.35 * k)
        return np.stack([cx + np.cos(ang) * rad, cy + np.sin(ang) * rad], 1)
    if origin == "zoom":
        return tgt + rng.normal(0, diag * 0.01, (n, 2))
    raise ValueError(origin)


def _delays(order: str, tgt: np.ndarray, W: float, H: float, rng) -> np.ndarray:
    n = len(tgt)
    if order == "together":
        return rng.uniform(0, 0.12, n)
    if order == "ltr":
        return np.clip(tgt[:, 0] / W + rng.normal(0, 0.03, n), 0, 1)
    if order == "ttb":
        return np.clip(tgt[:, 1] / H + rng.normal(0, 0.03, n), 0, 1)
    if order == "center_out":
        d = np.hypot(tgt[:, 0] - W / 2, tgt[:, 1] - H / 2)
        return np.clip(d / max(1e-6, d.max()) + rng.normal(0, 0.03, n), 0, 1)
    if order == "random":
        return rng.uniform(0, 1, n)
    raise ValueError(order)


# --------------------------------------------------------------------------
# 描画
# --------------------------------------------------------------------------
def _np_to_qimage(arr: np.ndarray) -> QImage:
    h, w = arr.shape[:2]
    rgba = np.empty((h, w, 4), dtype=np.uint8)
    rgba[..., :3] = arr
    rgba[..., 3] = 255
    return QImage(rgba.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()


@dataclass
class AnimationSettings:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    duration: float = 5.0      # 集まってくる時間（秒）
    hold: float = 1.5          # できあがった文字を見せる時間（秒）
    preset: int = 0
    seed: int = 0


class AnimationRenderer:
    """フレーム（1コマ）を作る係。frame(番号) で QImage を返す。"""

    def __init__(self, layout: mosaic_engine.TextLayout, tiles: dict[int, np.ndarray | None],
                 bg_color: tuple[int, int, int], st: AnimationSettings):
        self.st = st
        self.layout, self.tiles, self.bg_color = layout, tiles, tuple(bg_color)
        self.W, self.H = st.width, st.height
        self.bg = QColor(*bg_color)
        self.preset = PRESETS[st.preset % len(PRESETS)]
        cols, rows = layout.cols, layout.rows
        cw, ch = self.W / cols, self.H / rows
        self.cell_w, self.cell_h = cw, ch

        self.items: list[QImage] = []     # 文字の升目（飛んでくる）
        tgt = []
        self.bg_items: list[tuple[QRectF, QImage]] = []  # 背景の写真（最後にふわっと現れる）
        self.solid_text: list[tuple[QRectF, QColor]] = []
        for i in range(cols * rows):
            r, c = divmod(i, cols)
            rect = QRectF(c * cw, r * ch, cw + 0.6, ch + 0.6)  # 0.6px 重ねて隙間を防ぐ
            t = int(layout.assign[i])
            tile = tiles.get(t) if t >= 0 else None
            if tile is None:
                if layout.is_text[i]:
                    self.solid_text.append((rect, QColor(*[int(v) for v in layout.solid[i]])))
                continue
            a = float(layout.tint_alpha[i])
            arr = (tile * (1 - a) + layout.tint_rgb[i] * a).astype(np.uint8) if a > 0.004 else tile
            img = _np_to_qimage(arr)
            if layout.is_text[i]:
                self.items.append(img)
                tgt.append((c * cw, r * ch))
            else:
                self.bg_items.append((rect, img))
        if not self.items:
            raise MosaicError("文字の部分に写真がありません。", "タイル用の写真を追加してください。")
        self.tgt = np.array(tgt, dtype=np.float64)
        rng = np.random.default_rng(st.seed * 7919 + self.preset.index)
        self.start = _start_positions(self.preset.origin, self.tgt, self.W, self.H, rng)
        self.delay = _delays(self.preset.order, self.tgt + [cw / 2, ch / 2], self.W, self.H, rng)
        self.rot_sign = rng.choice([-1.0, 1.0], len(self.tgt))
        self.arc_amp = rng.uniform(0.15, 0.35, len(self.tgt)) * math.hypot(self.W, self.H) * rng.choice([-1, 1], len(self.tgt))
        spread = 0.12 if self.preset.order == "together" else 0.55
        self.move_frac = 1.0 - spread      # 1枚が飛ぶ時間（全体に対する割合）
        self.spread = spread
        self.n_anim = max(2, int(round(st.duration * st.fps)))
        self.n_hold = max(1, int(round(st.hold * st.fps)))

    def with_settings(self, st: AnimationSettings) -> "AnimationRenderer":
        """写真を読み直さずに、動き方・長さ・fps だけ変えた係を作る（同じ動画サイズに限る）。"""
        if (st.width, st.height) != (self.st.width, self.st.height):
            raise ValueError("動画サイズが違います")
        return AnimationRenderer(self.layout, self.tiles, self.bg_color, st)

    @property
    def frame_count(self) -> int:
        return self.n_anim + self.n_hold

    def _state(self, t: float):
        """全体の進み t（0〜1）での、各タイルの位置・回転・大きさ・不透明度。"""
        pr = self.preset
        p = np.clip((t - self.delay * self.spread) / self.move_frac, 0, 1)
        if pr.path == "bounce":
            e = _ease_out_back(p)
        else:
            e = _ease_out(p, pr.ease_power)
        pos = self.start + (self.tgt - self.start) * e[:, None]
        if pr.path == "arc":
            d = self.tgt - self.start
            nrm = np.stack([-d[:, 1], d[:, 0]], 1)
            ln = np.maximum(1e-6, np.hypot(nrm[:, 0], nrm[:, 1]))[:, None]
            pos = pos + nrm / ln * (np.sin(math.pi * np.clip(e, 0, 1)) * self.arc_amp * 0.5)[:, None]
        elif pr.path == "swirl":
            rem = (1 - np.clip(e, 0, 1))
            ang = rem * 2.5 * math.pi * self.rot_sign
            off = pos - self.tgt
            ca, sa = np.cos(ang), np.sin(ang)
            pos = self.tgt + np.stack([off[:, 0] * ca - off[:, 1] * sa, off[:, 0] * sa + off[:, 1] * ca], 1)
        rot = pr.rotation * (1 - np.clip(e, 0, 1)) * self.rot_sign
        scale = pr.start_scale + (1 - pr.start_scale) * np.clip(e, 0, 1.2)
        alpha = np.clip(p * 3, 0, 1) if pr.fade else np.where(p > 0, 1.0, 0.0 if pr.origin == "zoom" else 1.0)
        return p, pos, rot, scale, alpha

    def frame(self, k: int, scale_out: float = 1.0) -> QImage:
        """k 番目のフレーム。scale_out<1 でプレビュー用の小さい画像を作る。"""
        w, h = max(2, int(self.W * scale_out)), max(2, int(self.H * scale_out))
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(self.bg)
        t = min(1.0, k / max(1, self.n_anim - 1))
        p, pos, rot, scale, alpha = self._state(t)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.scale(scale_out, scale_out)
        # 背景の写真：最後の3割でふわっと現れる
        if self.bg_items:
            ba = float(np.clip((t - 0.7) / 0.3, 0, 1))
            if ba > 0:
                painter.setOpacity(ba)
                for rect, im in self.bg_items:
                    painter.drawImage(rect, im)
                painter.setOpacity(1.0)
        if self.solid_text and t >= 1.0:
            for rect, col in self.solid_text:
                painter.fillRect(rect, col)
        cw, ch = self.cell_w + 0.6, self.cell_h + 0.6
        order = np.argsort(-p)  # 着いたタイルを先に、飛んでいるタイルを上に描く
        for i in order:
            a = alpha[i]
            if a <= 0.01:
                continue
            x, y = pos[i]
            s = scale[i]
            if a < 0.999:
                painter.setOpacity(float(a))
            if abs(rot[i]) < 0.5 and abs(s - 1) < 0.01:
                painter.drawImage(QRectF(x, y, cw, ch), self.items[i])
            else:
                painter.save()
                painter.translate(x + cw / 2, y + ch / 2)
                painter.rotate(float(rot[i]))
                painter.scale(float(s), float(s))
                painter.drawImage(QRectF(-cw / 2, -ch / 2, cw, ch), self.items[i])
                painter.restore()
            if a < 0.999:
                painter.setOpacity(1.0)
        painter.end()
        return img


def build_renderer(text: str, mask_settings_base: text_mask.TextMaskSettings, records: list[PhotoRecord],
                   text_color, bg_color, bg_photos: bool, st: AnimationSettings,
                   progress: Callable[[int, int], None] | None = None,
                   is_cancelled: Callable[[], bool] | None = None) -> AnimationRenderer:
    """文字の配置を決め、使う写真を読み込んで、アニメーション係を用意する。"""
    long_side = 2400
    if st.width >= st.height:
        canvas = (long_side, round(long_side * st.height / st.width))
    else:
        canvas = (round(long_side * st.width / st.height), long_side)
    ms = text_mask.TextMaskSettings(
        font=mask_settings_base.font, canvas_size=canvas, weight=mask_settings_base.weight,
        letter_spacing=mask_settings_base.letter_spacing, line_spacing=mask_settings_base.line_spacing,
        multiline=mask_settings_base.multiline, margin=mask_settings_base.margin,
        font_size=None if mask_settings_base.font_size is None else int(mask_settings_base.font_size * canvas[1] / mask_settings_base.canvas_size[1]))
    res = text_mask.render_text_mask(text, ms)
    cols, rows = text_mask.auto_grid(res)
    tile_px = max(4, math.ceil(max(st.width / cols, st.height / rows)))
    s = mosaic_engine.TextMosaicSettings(cols=cols, rows=rows, tile_px=tile_px, text_color=tuple(text_color),
                                         bg_color=tuple(bg_color), bg_photos=bg_photos, seed=st.seed)
    lay = mosaic_engine.build_text_layout(text, ms, records, s)
    used = sorted(set(lay.assign[lay.assign >= 0].tolist()))
    tiles, _failed = mosaic_engine._load_tiles(records, used, tile_px, progress, is_cancelled, 0, 1000, 1000)
    return AnimationRenderer(lay, tiles, tuple(bg_color), st)
