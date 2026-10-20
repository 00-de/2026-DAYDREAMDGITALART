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
from PIL import Image, ImageFilter
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
    tile_level: int = 2        # 写真の大きさ（0＝細かい〜3＝特大）。初期値は「大きめ」
    fly_width: int = 150       # 飛んでくる途中の写真の横幅（px）。縦は元の写真の縦横比のまま。0＝マスと同じ


FLY_WIDTHS = [("マスと同じ", 0), ("100px", 100), ("150px（おすすめ）", 150), ("200px", 200), ("300px（迫力）", 300)]
FLY_BUDGET = 260_000_000   # 飛行用の写真（何段階かの大きさを含む）に使うメモリの上限（バイト）
SHARPEN = ImageFilter.UnsharpMask(radius=0.8, percent=45, threshold=2)


def _resize(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    out = img.resize(size, Image.Resampling.LANCZOS)
    return out.filter(SHARPEN) if min(size) >= 16 else out


class FlyImage:
    """1枚の写真の「飛行用」画像。元の縦横比のまま、大きさの違う版を何段階か持つ
    （大きく縮めるときも、近い大きさの版から描くので、ちらつかず、くっきり）。"""

    def __init__(self, pil: Image.Image, tile: int):
        self.w0, self.h0 = pil.size
        self.levels: list[tuple[float, QImage]] = []  # (倍率, 画像)  倍率＝元の幅に対する比
        k = 1.0
        im = pil
        while True:
            self.levels.append((k, _pil_to_qimage(im)))
            nw, nh = im.width // 2, im.height // 2
            if min(nw, nh) < tile:
                break
            im = _resize(im, (nw, nh))
            k /= 2
        side = min(self.w0, self.h0)
        self.crop = ((self.w0 - side) / 2, (self.h0 - side) / 2, side)  # 着地時の正方形（中央）
        sq = pil.crop((int(self.crop[0]), int(self.crop[1]), int(self.crop[0]) + side, int(self.crop[1]) + side))
        self.exact = _pil_to_qimage(sq if side == tile else _resize(sq, (tile, tile)))

    def pick(self, draw_w: float, src_w: float) -> tuple[float, QImage]:
        """画面に draw_w の幅で描くとき、ちょうどよい大きさの版を選ぶ。"""
        need = draw_w / max(1e-6, src_w)  # 元画像1pxあたりの画面px
        best = self.levels[0]
        for k, im in self.levels:
            if k >= need * 0.95:
                best = (k, im)
        return best


def _pil_to_qimage(im: Image.Image) -> QImage:
    return _np_to_qimage(np.asarray(im.convert("RGB"), dtype=np.uint8))


def load_fly_images(records: list[PhotoRecord], used: list[int], tile: int, fly_width: int,
                    progress: Callable[[int, int], None] | None = None,
                    is_cancelled: Callable[[], bool] | None = None) -> tuple[dict[int, Image.Image], list[str]]:
    """使う写真を、元の縦横比のまま読み込む（横幅＝飛んでくる大きさ。最低でも1マス分は確保）。"""
    from concurrent.futures import ThreadPoolExecutor

    from . import image_io
    n = max(1, len(used))
    # メモリの上限から、1枚あたりに使える画素数（4/3 は段階版のぶん）
    per_px = FLY_BUDGET / 4 / n / (4 / 3)
    out: dict[int, Image.Image] = {}
    failed: list[str] = []

    def load(i: int):
        try:
            li = image_io.load_image(records[i].path, max_side=max(fly_width, tile) * 3)
            im = li.image
            ar = im.height / im.width
            w = max(fly_width, 1)
            w = min(w, int(math.sqrt(per_px / ar)))         # メモリの上限
            w = max(w, int(math.ceil(tile / min(1.0, ar))))  # 着地用の正方形が1マス以上になるように
            w = min(w, im.width) if im.width >= tile and min(im.width, im.height) >= tile else w
            h = max(1, round(w * ar))
            return i, (im if (w, h) == im.size else _resize(im, (w, h)))
        except Exception:
            return i, None

    with ThreadPoolExecutor(max_workers=8) as ex:
        for k, (i, im) in enumerate(ex.map(load, used)):
            if im is None:
                failed.append(records[i].path)
            else:
                out[i] = im
            if k % 20 == 0:
                if is_cancelled and is_cancelled():
                    raise mosaic_engine.GenerationCancelled("中止しました。")
                if progress:
                    progress(k, len(used))
    return out, failed


class AnimationRenderer:
    """フレーム（1コマ）を作る係。frame(番号) で QImage を返す。"""

    def __init__(self, layout: mosaic_engine.TextLayout, tiles: dict[int, np.ndarray | None],
                 bg_color: tuple[int, int, int], st: AnimationSettings, tile: int | None = None,
                 offset: tuple[int, int] = (0, 0), _cache: dict | None = None,
                 overlay: tuple[QImage, float] | None = None, cell_images: dict[int, QImage] | None = None):
        """tiles：写真ごとの飛行用画像（元の縦横比の PIL 画像）。tile：動画上の1マスの大きさ（整数px）。
        cell_images：升目ごとの着地画像（写真モザイクで、メイン画像に合わせて変形した写真）。"""
        self.st = st
        self.layout, self.tiles, self.bg_color = layout, tiles, tuple(bg_color)
        self.W, self.H = st.width, st.height
        self.bg = QColor(*bg_color)
        self.preset = PRESETS[st.preset % len(PRESETS)]
        cols, rows = layout.cols, layout.rows
        if tile is None:  # 古い呼び出し方との互換
            tile = max(1, int(min(self.W / cols, self.H / rows)))
        self.tile, self.offset = tile, offset
        self.overlay = overlay  # 写真モザイク：最後に参考画像をうっすら重ねる（輪郭の強調）
        self.cell_images = cell_images
        self.cell_w = self.cell_h = float(tile)
        ox, oy = offset

        # 写真ごとに「着地用のぴったりサイズ」と「飛行用（縦横比そのまま・段階版つき）」を1回だけ作る
        self._cache = _cache if _cache is not None else {}
        if "fly" not in self._cache:
            self._cache["fly"] = {t: FlyImage(im, tile) for t, im in tiles.items() if im is not None}
        self.fly: dict[int, FlyImage] = self._cache["fly"]
        self.exact = {t: f.exact for t, f in self.fly.items()}

        self.items: list[tuple[int, QColor | None]] = []   # 文字の升目：(写真番号, 色寄せ)
        self.item_img: list[QImage | None] = []            # 着地画像（変形した写真。無ければ写真そのもの）
        tgt = []
        self.bg_items: list[tuple[QRectF, int, QColor | None]] = []
        self.solid_text: list[tuple[QRectF, QColor]] = []
        for i in range(cols * rows):
            r, c = divmod(i, cols)
            x, y = ox + c * tile, oy + r * tile
            rect = QRectF(x, y, tile, tile)
            t = int(layout.assign[i])
            if t < 0 or t not in self.exact:
                if layout.is_text[i]:
                    self.solid_text.append((rect, QColor(*[int(v) for v in layout.solid[i]])))
                continue
            a = float(layout.tint_alpha[i])
            tint = QColor(*[int(v) for v in layout.tint_rgb[i]], int(round(a * 255))) if a > 0.004 else None
            if layout.is_text[i]:
                cimg = cell_images.get(i) if cell_images else None
                self.items.append((t, None if cimg is not None else tint))
                self.item_img.append(cimg)
                tgt.append((x, y))
            else:
                self.bg_items.append((rect, t, tint))
        if not self.items:
            raise MosaicError("文字の部分に写真がありません。", "タイル用の写真を追加してください。")
        self.tgt = np.array(tgt, dtype=np.float64)
        rng = np.random.default_rng(st.seed * 7919 + self.preset.index)
        self.start = _start_positions(self.preset.origin, self.tgt, self.W, self.H, rng)
        self.delay = _delays(self.preset.order, self.tgt + [tile / 2, tile / 2], self.W, self.H, rng)
        self.rot_sign = rng.choice([-1.0, 1.0], len(self.tgt))
        self.arc_amp = rng.uniform(0.15, 0.35, len(self.tgt)) * math.hypot(self.W, self.H) * rng.choice([-1, 1], len(self.tgt))
        spread = 0.12 if self.preset.order == "together" else 0.55
        self.move_frac = 1.0 - spread      # 1枚が飛ぶ時間（全体に対する割合）
        self.spread = spread
        self.n_anim = max(2, int(round(st.duration * st.fps)))
        self.n_hold = max(1, int(round(st.hold * st.fps)))

    def with_settings(self, st: AnimationSettings) -> "AnimationRenderer":
        """写真を読み直さずに、動き方・長さ・fps だけ変えた係を作る（同じ動画サイズに限る）。"""
        if (st.width, st.height, st.tile_level) != (self.st.width, self.st.height, self.st.tile_level):
            raise ValueError("動画サイズ・写真の大きさが違います")
        r = AnimationRenderer(self.layout, self.tiles, self.bg_color, st, self.tile, self.offset, self._cache,
                              self.overlay, self.cell_images)
        r.warnings = getattr(self, "warnings", [])
        r.closeness = getattr(self, "closeness", None)
        return r

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
        T = float(self.tile)
        ps = np.clip(p, 0, 1)
        if self.st.fly_width > 0:
            # 飛んでくる途中は「横幅 fly_width・元の縦横比」、着地に近づくとゆっくり1マスの正方形へ
            start_w = max(T, self.st.fly_width * min(3.0, max(1.0, pr.start_scale)))
            kk = 1 - ps ** 2.2
            width = T + (start_w - T) * kk
            if pr.path == "bounce":  # はずむ動きは着地の直前に少しだけ大きくなる
                width = width * (1 + 0.15 * np.clip(e - 1, 0, None) * 5)
            mix = kk                 # 1＝写真全体、0＝中央の正方形
        else:
            width = T * (pr.start_scale + (1 - pr.start_scale) * np.clip(e, 0, 1.2))
            mix = np.zeros_like(ps)
        alpha = np.clip(p * 3, 0, 1) if pr.fade else np.where(p > 0, 1.0, 0.0 if pr.origin == "zoom" else 1.0)
        return p, pos, rot, width, mix, alpha

    def frame(self, k: int, scale_out: float = 1.0) -> QImage:
        """k 番目のフレーム。scale_out<1 でプレビュー用の小さい画像を作る。"""
        w, h = max(2, int(self.W * scale_out)), max(2, int(self.H * scale_out))
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(self.bg)
        t = min(1.0, k / max(1, self.n_anim - 1))
        p, pos, rot, width, mix, alpha = self._state(t)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.scale(scale_out, scale_out)
        # 背景の写真：最後の3割でふわっと現れる
        if self.bg_items:
            ba = float(np.clip((t - 0.7) / 0.3, 0, 1))
            if ba > 0:
                painter.setOpacity(ba)
                for rect, ti, tint in self.bg_items:
                    painter.drawImage(rect, self.exact[ti])
                    if tint is not None:
                        painter.fillRect(rect, tint)
                painter.setOpacity(1.0)
        if self.solid_text and t >= 1.0:
            for rect, col in self.solid_text:
                painter.fillRect(rect, col)
        T = float(self.tile)
        order = np.argsort(-p)  # 着いたタイルを先に、飛んでいるタイルを上に描く
        for i in order:
            a = alpha[i]
            if a <= 0.01:
                continue
            x, y = pos[i]
            w, m = float(width[i]), float(mix[i])
            ti, tint = self.items[i]
            if a < 0.999:
                painter.setOpacity(float(a))
            if abs(rot[i]) < 0.5 and abs(w - T) < 0.5 and m < 0.01:
                # 着地：ぴったりサイズの正方形を整数位置に描く（くっきり）
                rect = QRectF(round(x), round(y), T, T)
                painter.drawImage(rect, self.item_img[i] if self.item_img[i] is not None else self.exact[ti])
                if tint is not None:
                    painter.fillRect(rect, tint)
            else:
                # 飛行中：写真全体（縦横比そのまま）→ 中央の正方形へ、なめらかに切り替える
                f = self.fly[ti]
                cx, cy, side = f.crop
                sx, sy = cx * (1 - m), cy * (1 - m)
                sw = side + (f.w0 - side) * m
                sh = side + (f.h0 - side) * m
                h = w * sh / sw
                k, src_img = f.pick(w, sw)
                painter.save()
                painter.translate(x + T / 2, y + T / 2)
                if abs(rot[i]) >= 0.5:
                    painter.rotate(float(rot[i]))
                rect = QRectF(-w / 2, -h / 2, w, h)
                painter.drawImage(rect, src_img, QRectF(sx * k, sy * k, sw * k, sh * k))
                if tint is not None:
                    painter.fillRect(rect, tint)
                cimg = self.item_img[i]
                if cimg is not None and m < FIT_FADE:  # 着地の直前に、変形した写真へなめらかに切り替える
                    painter.setOpacity(float(a) * (1 - m / FIT_FADE))
                    painter.drawImage(rect, cimg)
                    painter.setOpacity(float(a))
                painter.restore()
            if a < 0.999:
                painter.setOpacity(1.0)
        if self.overlay is not None:
            ov, strength = self.overlay
            oa = float(np.clip((t - 0.7) / 0.3, 0, 1)) * strength
            if oa > 0.004:
                painter.setOpacity(oa)
                ox, oy = self.offset
                painter.drawImage(QRectF(ox, oy, self.layout.cols * T, self.layout.rows * T), ov)
                painter.setOpacity(1.0)
        painter.end()
        return img


def build_renderer(text: str, mask_settings_base: text_mask.TextMaskSettings, records: list[PhotoRecord],
                   text_color, bg_color, bg_photos: bool, st: AnimationSettings,
                   progress: Callable[[int, int], None] | None = None,
                   is_cancelled: Callable[[], bool] | None = None) -> AnimationRenderer:
    """文字の配置を決め、使う写真を読み込んで、アニメーション係を用意する。

    1マスの大きさを整数ピクセルにそろえ、余りは上下左右の余白にする（写真がにじまない）。
    """
    def mask_settings(canvas):
        b = mask_settings_base
        fs = None if b.font_size is None else int(b.font_size * canvas[1] / b.canvas_size[1])
        return text_mask.TextMaskSettings(font=b.font, canvas_size=canvas, weight=b.weight,
                                          letter_spacing=b.letter_spacing, line_spacing=b.line_spacing,
                                          multiline=b.multiline, margin=b.margin, font_size=fs)

    long_side = 2400
    if st.width >= st.height:
        canvas = (long_side, round(long_side * st.height / st.width))
    else:
        canvas = (round(long_side * st.width / st.height), long_side)
    res = text_mask.render_text_mask(text, mask_settings(canvas))
    cols0, rows0 = text_mask.auto_grid(res, st.tile_level)
    tile = max(6, int(min(st.width / cols0, st.height / rows0)))
    cols, rows = st.width // tile, st.height // tile
    offset = ((st.width - cols * tile) // 2, (st.height - rows * tile) // 2)
    m = max(1, round(long_side / max(cols, rows)))
    ms = mask_settings((cols * m, rows * m))

    s = mosaic_engine.TextMosaicSettings(cols=cols, rows=rows, tile_px=tile, text_color=tuple(text_color),
                                         bg_color=tuple(bg_color), bg_photos=bg_photos, seed=st.seed)
    lay = mosaic_engine.build_text_layout(text, ms, records, s)
    used = sorted(set(lay.assign[lay.assign >= 0].tolist()))
    tiles, _failed = load_fly_images(records, used, tile, st.fly_width, progress, is_cancelled)
    return AnimationRenderer(lay, tiles, tuple(bg_color), st, tile, offset)


# --------------------------------------------------------------------------
# 写真モザイクの集合アニメーション：写真が飛んできて、参考画像（メイン写真）のモザイクになる
# --------------------------------------------------------------------------
# 「写真の大きさ」ごとの、参考画像の短い辺に並べるタイル数（細かい → 特大）
PHOTO_LEVEL_SHORT = [80, 60, 45, 32]
FIT_FADE = 0.35   # 着地のどれくらい手前から、変形した写真に切り替え始めるか（mix の値）


def build_photo_renderer(main_photo_path: str, records: list[PhotoRecord], st: AnimationSettings,
                         max_uses: int = 5, color_match: int = 70, edge: int = 20,
                         bg_color: tuple[int, int, int] = (30, 26, 46),
                         progress: Callable[[int, int], None] | None = None,
                         is_cancelled: Callable[[], bool] | None = None, fit: int = 0) -> AnimationRenderer:
    """参考画像を動画の中に収まる大きさで置き、その形・色になるように写真を集める。

    fit：メイン画像への近さの目標（%）。0 以外なら、写真を升目ごとに変形してメイン画像に合わせる。
    """
    from . import image_io
    from .color import srgb_to_lab  # noqa: F401  （色の計算は mosaic_engine と共通）
    if not records:
        raise MosaicError("タイル用の写真が登録されていません。", "① 写真コレクションに写真を追加してください。")
    W, H = st.width, st.height
    main = image_io.load_image(main_photo_path, max_side=max(W, H))
    aw, ah = main.image.size
    k = min(W / aw, H / ah)                      # 動画の中に収まる大きさ（縦横比はそのまま）
    fw, fh = aw * k, ah * k
    n_short = PHOTO_LEVEL_SHORT[max(0, min(st.tile_level, len(PHOTO_LEVEL_SHORT) - 1))]
    tile = max(6, int(min(fw, fh) / n_short))
    cols, rows = max(1, int(fw // tile)), max(1, int(fh // tile))
    offset = ((W - cols * tile) // 2, (H - rows * tile) // 2)

    cell_feats, cell_rgb = mosaic_engine._cell_features(main.image, cols, rows)
    tile_feats = np.array([r.grid_lab for r in records], dtype=np.float32)
    n_cells = cols * rows
    uses = mosaic_engine._effective_max_uses(n_cells, len(records), max_uses)
    rng = np.random.default_rng(st.seed)
    assign = mosaic_engine.assign_tiles(cell_feats, tile_feats, cols, rows, uses, rng, cancelled=is_cancelled)
    alpha = 0.0 if fit else 0.55 * (color_match / 100) ** 1.5
    lay = mosaic_engine.TextLayout(
        cols=cols, rows=rows, assign=assign, is_text=np.ones(n_cells, dtype=bool),
        tint_rgb=cell_rgb.astype(np.float32), tint_alpha=np.full(n_cells, alpha, dtype=np.float32),
        solid=cell_rgb.astype(np.float32), used_kinds=len(set(assign.tolist())), mask_result=None,
        warnings=([f"写真の枚数に対してタイルが多いため、同じ写真を最大{uses}回使いました。"]
                  if mosaic_engine._required_uses(n_cells, len(records)) > max_uses else []))
    used = sorted(set(assign.tolist()))
    tiles, _failed = load_fly_images(records, used, tile, st.fly_width, progress, is_cancelled)
    from PIL import ImageOps
    ov = ImageOps.fit(main.image, (cols * tile, rows * tile), Image.Resampling.LANCZOS)
    overlay = (_pil_to_qimage(ov), 0.4 * edge / 100) if edge > 0 else None
    cell_images, closeness = None, None
    if fit:
        cell_images, closeness = _fitted_cells(lay, tiles, tile, np.asarray(ov, dtype=np.uint8), edge, fit,
                                               is_cancelled)
        overlay = None  # 輪郭の強調は、変形した写真の中に含めた
    r = AnimationRenderer(lay, tiles, tuple(bg_color), st, tile, offset, overlay=overlay, cell_images=cell_images)
    r.warnings = lay.warnings
    r.closeness = closeness
    return r


def _fitted_cells(lay: mosaic_engine.TextLayout, tiles: dict[int, Image.Image], tile: int, target: np.ndarray,
                  edge: int, fit: int, is_cancelled) -> tuple[dict[int, QImage], float]:
    """完成のモザイクを静止画と同じ方法で作り、写真をメイン画像に合わせて変形 → 升目ごとの着地画像にする。"""
    cols, rows = lay.cols, lay.rows
    squares: dict[int, np.ndarray] = {}
    for t, im in tiles.items():
        side = min(im.size)
        x0, y0 = (im.width - side) // 2, (im.height - side) // 2
        sq = im.crop((x0, y0, x0 + side, y0 + side))
        squares[t] = np.asarray(sq if side == tile else _resize(sq, (tile, tile)), dtype=np.uint8)
    out = np.empty((rows * tile, cols * tile, 3), dtype=np.uint8)
    for i in range(cols * rows):
        r, c = divmod(i, cols)
        t = int(lay.assign[i])
        out[r * tile:(r + 1) * tile, c * tile:(c + 1) * tile] = squares[t] if t in squares else lay.solid[i]
    if edge > 0:
        ea = 0.4 * edge / 100
        out = (out * (1 - ea) + target * ea).astype(np.uint8)
    closeness = mosaic_engine.fit_to_target(out, target, tile, fit, is_cancelled)
    cells: dict[int, QImage] = {}
    for i in range(cols * rows):
        r, c = divmod(i, cols)
        cells[i] = _np_to_qimage(np.ascontiguousarray(out[r * tile:(r + 1) * tile, c * tile:(c + 1) * tile]))
    return cells, closeness
