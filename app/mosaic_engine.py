"""モザイク生成エンジン（フェーズ6）。写真モザイクと文字モザイクの両方を作る。

■ 写真モザイクの作り方
  1. メイン写真をタイルの升目に分け、各升目を 2×2 の小区画に分けて色（Lab）を調べる
  2. 各写真の色（同じく 2×2 区画）と比べ、色の差が小さい写真を候補にする
     → 平均色だけでなく「上が明るく下が暗い」などの色の配置も似ている写真が選ばれる
  3. 升目をランダムな順番で埋めていき、次のルールで写真を決める
       ・同じ写真の使用回数の上限を守る
       ・すぐ隣（上下左右・斜め）に同じ写真を置かない
  4. 「色の一致度」に応じて、各タイルを升目の色に少しだけ寄せる（元の写真に近づく）
  5. 「輪郭の強調」に応じて、元の写真をうっすら重ねる（顔や形がはっきりする）
  6. 「メイン画像への近さ」（初期値 99%）がオンのときは、写真を1枚ずつ変形してメイン画像に合わせる
       ・色と明るさ・コントラストを、その升目のメイン画像に合わせる
       ・升目の中の明暗（例：左半分が暗い）も、メイン画像と同じになるように整える
         （写真の細かい模様＝写っている人や景色は残す）
       ・最後に、全体表示で見たときの近さが目標（99% など）に届くよう、メイン画像を必要なだけ重ねる
     近さ＝全体表示（長い辺 1,600px）で比べた色の差から計算（100% − 平均の色の差 ÷ 255）

■ 文字モザイクの作り方
  1. 文字マスク（白黒画像）をタイルの升目に分け、文字が半分以上かかる升目を「文字の升目」にする
  2. 文字の升目に写真を、背景に単色（または色を寄せた写真）を配置する
  3. 背景が暗いときは明るい写真、明るいときは暗い写真を優先し、文字を読みやすくする
     さらに背景との明るさの差が小さい写真は、文字色に寄せて読みやすさを補助する

どちらも同じ「乱数の種（シード）」を使えば、同じ配置を再現できる（設定情報に保存）。
"""

from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from . import config, image_io, sysinfo, text_mask
from .collection import PhotoRecord
from .color import srgb_to_lab
from .errors import MosaicError

PREVIEW_SIDE = 1600


class GenerationCancelled(MosaicError):
    pass


@dataclass
class PhotoMosaicSettings:
    cols: int = 100
    rows: int = 75
    tile_px: int = 40
    max_uses: int = 5
    color_match: int = 70      # 0〜100：色寄せの強さ
    edge: int = 20             # 0〜100：元の写真を重ねる強さ
    border: bool = False
    seed: int = 0
    fit: int = 99              # 0＝オフ／90〜99：メイン画像への近さの目標（%）。写真を変形して合わせる


# 「メイン画像への近さ」の選択肢（値, 表示名）
# 99%〜90% を 1% きざみで選べる（数字が小さいほど、拡大したときに写真の模様がよく見える）
_FIT_NOTES = {99: "（写真を変形してメイン画像にそっくり）", 95: "（写真の模様がよく見える）",
              90: "（写真の模様がいちばんよく見える）"}
FIT_LEVELS: list[tuple[int, str]] = (
    [(v, f"{v}%{_FIT_NOTES.get(v, '')}") for v in range(99, 89, -1)]
    + [(0, "オフ（写真の色のまま・以前と同じ）")]
)


@dataclass
class TextMosaicSettings:
    cols: int = 120
    rows: int = 80
    tile_px: int = 40
    text_color: tuple[int, int, int] = (255, 255, 255)
    bg_color: tuple[int, int, int] = (30, 26, 46)
    bg_photos: bool = False    # 背景も写真で埋める
    threshold: float = 0.5     # 升目の何割に文字がかかったら「文字の升目」にするか
    seed: int = 0


@dataclass
class MosaicResult:
    image: Image.Image
    preview: Image.Image
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    settings: dict = field(default_factory=dict)   # 再現用の設定情報


Progress = Callable[[int, int], None]
Cancelled = Callable[[], bool]


# --------------------------------------------------------------------------
# 共通チェック
# --------------------------------------------------------------------------
def estimate_memory_bytes(width: int, height: int) -> int:
    return width * height * 3 * 3  # 完成画像＋重ね合わせ用＋作業用


def check_output_size(width: int, height: int) -> None:
    if width * height > config.MAX_OUTPUT_PIXELS:
        raise MosaicError(f"出力画像が大きすぎます（{width:,}×{height:,}）。",
                          "タイルの数か、タイル1枚の大きさを減らしてください。")
    need = estimate_memory_bytes(width, height)
    avail = sysinfo.available_memory_bytes()
    if avail is not None and need > avail * 0.8:
        raise MosaicError(
            f"メモリが足りない可能性があります（必要 約{need / 1e9:.1f}GB／空き 約{avail / 1e9:.1f}GB）。",
            "他のアプリを閉じるか、タイルの数・大きさを減らしてください。")


def _check(cancelled: Cancelled | None) -> None:
    if cancelled and cancelled():
        raise GenerationCancelled("生成を中止しました。")


def _required_uses(n_cells: int, n_tiles: int) -> int:
    """全升目を埋めるのに最低限必要な、1枚あたりの使用回数。"""
    return math.ceil(n_cells / max(1, n_tiles))


def _effective_max_uses(n_cells: int, n_tiles: int, requested: int) -> int:
    """指定の回数で足りればそのまま。足りないときだけ、少し余裕を持たせて増やす。"""
    if requested * n_tiles >= n_cells:
        return max(1, requested)
    return max(1, math.ceil(n_cells * 1.05 / max(1, n_tiles)))


def _neighbors(idx: int, cols: int, rows: int):
    r, c = divmod(idx, cols)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            rr, cc = r + dr, c + dc
            if 0 <= rr < rows and 0 <= cc < cols:
                yield rr * cols + cc


# --------------------------------------------------------------------------
# 写真の割り当て（写真モザイク）
# --------------------------------------------------------------------------
def _cell_features(img: Image.Image, cols: int, rows: int) -> tuple[np.ndarray, np.ndarray]:
    """メイン写真から、各升目の 2×2 区画の Lab（[升目数, 12]）と平均RGB（[升目数, 3]）を作る。"""
    fitted = ImageOps.fit(img, (cols * 2, rows * 2), Image.Resampling.LANCZOS)
    lab = srgb_to_lab(np.asarray(fitted))                       # [rows*2, cols*2, 3]
    grid = lab.reshape(rows, 2, cols, 2, 3).transpose(0, 2, 1, 3, 4).reshape(rows * cols, 12)
    rgb = np.asarray(fitted.resize((cols, rows), Image.Resampling.BOX), dtype=np.float32).reshape(-1, 3)
    return grid.astype(np.float32), rgb


def assign_tiles(cell_feats: np.ndarray, tile_feats: np.ndarray, cols: int, rows: int,
                 max_uses: int, rng: np.random.Generator,
                 progress: Progress | None = None, cancelled: Cancelled | None = None) -> np.ndarray:
    """各升目に写真を割り当てる。戻り値：写真の番号（形 [升目数]）。"""
    n_cells, n_tiles = len(cell_feats), len(tile_feats)
    k = min(n_tiles, 48)
    # 色の差の2乗 = |a|² + |b|² − 2a·b（まとめて高速に計算）
    t2 = (tile_feats ** 2).sum(axis=1)
    cand = np.empty((n_cells, k), dtype=np.int32)
    step = 2048
    for s in range(0, n_cells, step):
        _check(cancelled)
        c = cell_feats[s:s + step]
        d = (c ** 2).sum(axis=1)[:, None] + t2[None, :] - 2 * c @ tile_feats.T
        part = np.argpartition(d, k - 1, axis=1)[:, :k] if k < n_tiles else np.tile(np.arange(n_tiles), (len(c), 1))
        order = np.take_along_axis(d, part, axis=1).argsort(axis=1)
        cand[s:s + step] = np.take_along_axis(part, order, axis=1)

    uses = np.zeros(n_tiles, dtype=np.int32)
    out = np.full(n_cells, -1, dtype=np.int32)
    for i, idx in enumerate(rng.permutation(n_cells)):
        if i % 2000 == 0:
            _check(cancelled)
            if progress:
                progress(i, n_cells)
        near = {out[j] for j in _neighbors(idx, cols, rows)}
        choice = -1
        for t in cand[idx]:
            if uses[t] < max_uses and t not in near:
                choice = t
                break
        if choice < 0:  # 候補が使い切られていたら、全写真から探し直す
            d = ((tile_feats - cell_feats[idx]) ** 2).sum(axis=1)
            for t in np.argsort(d):
                if uses[t] < max_uses and t not in near:
                    choice = int(t)
                    break
        if choice < 0:  # 写真がとても少ない場合は「隣に同じ写真」を許す
            avail = np.where(uses < max_uses)[0]
            pool = avail if len(avail) else np.arange(n_tiles)
            d = ((tile_feats[pool] - cell_feats[idx]) ** 2).sum(axis=1)
            choice = int(pool[int(np.argmin(d))])
        out[idx] = choice
        uses[choice] += 1
    if progress:
        progress(n_cells, n_cells)
    return out


# --------------------------------------------------------------------------
# 写真の割り当て（文字モザイク）
# --------------------------------------------------------------------------
def assign_text_tiles(text_cells: np.ndarray, bg_cells: np.ndarray, records: list[PhotoRecord],
                      cols: int, rows: int, bg_lab_l: float, bg_photos: bool,
                      rng: np.random.Generator) -> tuple[np.ndarray, int]:
    """文字の升目（と背景の升目）に写真を割り当てる。戻り値：(写真番号 [升目数]、-1は単色), 使った写真の種類数。"""
    n_cells = cols * rows
    out = np.full(n_cells, -1, dtype=np.int32)
    lightness = np.array([r.mean_lab[0] for r in records])
    order = np.argsort(lightness)              # 暗い → 明るい
    if bg_lab_l < 50:
        order = order[::-1]                    # 背景が暗い → 明るい写真を文字に
    n = len(records)
    if bg_photos and n >= 4:
        split = max(1, int(n * 0.6))
        text_pool, bg_pool = order[:split], order[split:]
    else:
        keep = n if n < 30 else max(30, int(n * 0.7))  # 背景と明るさの差が大きい写真を優先
        text_pool, bg_pool = order[:keep], order

    def fill(cells: np.ndarray, pool: np.ndarray) -> None:
        pool = rng.permutation(pool)
        pos = 0
        for idx in cells:
            near = {out[j] for j in _neighbors(int(idx), cols, rows)}
            for attempt in range(len(pool)):
                t = pool[(pos + attempt) % len(pool)]
                if t not in near or attempt == len(pool) - 1:
                    out[idx] = t
                    pos = (pos + attempt + 1) % len(pool)
                    break

    fill(text_cells, text_pool)
    if bg_photos:
        fill(bg_cells, bg_pool)
    used = len(set(out[out >= 0].tolist()))
    return out, used


# --------------------------------------------------------------------------
# 画像の組み立て
# --------------------------------------------------------------------------
def _load_tiles(records: list[PhotoRecord], used: list[int], tile_px: int,
                progress: Progress | None, cancelled: Cancelled | None,
                base: int, span: int, total: int) -> tuple[dict[int, np.ndarray | None], list[str]]:
    """使う写真だけをタイルの大きさで読み込む（並列）。"""
    tiles: dict[int, np.ndarray | None] = {}
    failed: list[str] = []

    def load(i: int):
        try:
            li = image_io.load_image(records[i].path, max_side=tile_px * 2)
            sq = ImageOps.fit(li.image, (tile_px, tile_px), Image.Resampling.LANCZOS)
            if tile_px >= 16:  # 縮小でぼやけた輪郭を軽く引き締める（写真がくっきり見える）
                sq = sq.filter(ImageFilter.UnsharpMask(radius=max(0.6, tile_px / 90), percent=45, threshold=2))
            return i, np.asarray(sq, dtype=np.uint8)
        except Exception:
            return i, None

    with ThreadPoolExecutor(max_workers=8) as ex:
        for n, (i, arr) in enumerate(ex.map(load, used)):
            tiles[i] = arr
            if arr is None:
                failed.append(records[i].path)
            if n % 20 == 0:
                _check(cancelled)
                if progress:
                    progress(base + int(span * n / max(1, len(used))), total)
    return tiles, failed


def _compose(assign: np.ndarray, cols: int, rows: int, tile_px: int,
             tiles: dict[int, np.ndarray | None], tint_rgb: np.ndarray, tint_alpha: np.ndarray,
             solid_rgb: np.ndarray, progress: Progress | None, cancelled: Cancelled | None,
             base: int, span: int, total: int) -> np.ndarray:
    H, W = rows * tile_px, cols * tile_px
    out = np.empty((H, W, 3), dtype=np.uint8)
    for r in range(rows):
        if r % 4 == 0:
            _check(cancelled)
            if progress:
                progress(base + int(span * r / rows), total)
        y = r * tile_px
        for c in range(cols):
            i = r * cols + c
            x = c * tile_px
            t = assign[i]
            tile = tiles.get(int(t)) if t >= 0 else None
            if tile is None:
                out[y:y + tile_px, x:x + tile_px] = solid_rgb[i]
                continue
            a = tint_alpha[i]
            if a > 0.004:
                out[y:y + tile_px, x:x + tile_px] = (tile * (1 - a) + tint_rgb[i] * a).astype(np.uint8)
            else:
                out[y:y + tile_px, x:x + tile_px] = tile
    return out


# --------------------------------------------------------------------------
# 写真の変形：メイン画像に合わせる
# --------------------------------------------------------------------------
def _lowpass_tiles(a: np.ndarray, k: int) -> np.ndarray:
    """タイルごとの「大まかな明暗」（k×k の区画の平均をなめらかにつないだもの）。a: [枚数, t, t, 3]"""
    t = a.shape[1]
    k = max(1, min(k, t))
    edges = np.linspace(0, t, k + 1).round().astype(int)
    s = np.add.reduceat(np.add.reduceat(a, edges[:-1], axis=1), edges[:-1], axis=2)
    cnt = np.diff(edges).astype(np.float32)
    pooled = s / (cnt[None, :, None, None] * cnt[None, None, :, None])          # [枚数, k, k, 3]
    wm = np.zeros((t, k), dtype=np.float32)
    if k == 1:
        wm[:, 0] = 1
    else:
        centers = (edges[:-1] + edges[1:] - 1) / 2
        pos = np.arange(t)
        idx = np.clip(np.searchsorted(centers, pos) - 1, 0, k - 2)
        w = np.clip((pos - centers[idx]) / (centers[idx + 1] - centers[idx]), 0, 1).astype(np.float32)
        wm[pos, idx] += 1 - w
        wm[pos, idx + 1] += w
    return np.einsum("yk,nklc,xl->nyxc", wm, pooled, wm, optimize=True)


def fit_tiles_to_target(tiles: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """写真（タイル）を、同じ位置のメイン画像に合わせて変形する。どちらも [枚数, t, t, 3]。

    1) 色・明るさ・コントラストをメイン画像の升目に合わせる
    2) 升目の中の大まかな明暗をメイン画像と同じにする（写真の細かい模様は残す）
    """
    P = tiles.astype(np.float32)
    T = targets.astype(np.float32)
    mP, mT = P.mean(axis=(1, 2), keepdims=True), T.mean(axis=(1, 2), keepdims=True)
    sP, sT = P.std(axis=(1, 2), keepdims=True), T.std(axis=(1, 2), keepdims=True)
    gain = np.clip((sT + 6) / (sP + 6), 0.35, 1.6)   # 平らな升目でも写真の模様が消えすぎないように
    X = (P - mP) * gain + mT
    t = P.shape[1]
    k = 4 if t >= 12 else 2 if t >= 4 else 1
    X = X - _lowpass_tiles(X, k) + _lowpass_tiles(T, k)
    return np.clip(X, 0, 255)


def _to_tiles(strip: np.ndarray, t: int) -> np.ndarray:
    """[t, 列数*t, 3] の帯 → [列数, t, t, 3]"""
    cols = strip.shape[1] // t
    return strip[:, :cols * t].reshape(t, cols, t, 3).transpose(1, 0, 2, 3)


def _from_tiles(tiles: np.ndarray) -> np.ndarray:
    n, t = tiles.shape[:2]
    return tiles.transpose(1, 0, 2, 3).reshape(t, n * t, 3)


def similarity(img: np.ndarray, target: np.ndarray) -> float:
    """全体表示（長い辺 1,600px）で比べた近さ（%）。100% − 平均の色の差 ÷ 255。"""
    a, b = _preview_arrays(img, target)
    return float(100.0 * (1.0 - np.abs(a - b).mean() / 255.0))


def _preview_arrays(img: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    h, w = img.shape[:2]
    k = min(1.0, PREVIEW_SIDE / max(w, h))
    size = (max(1, round(w * k)), max(1, round(h * k)))
    a = np.asarray(Image.fromarray(img).resize(size, Image.Resampling.BOX), dtype=np.float32)
    b = np.asarray(Image.fromarray(target).resize(size, Image.Resampling.BOX), dtype=np.float32)
    return a, b


def fit_to_target(out: np.ndarray, target: np.ndarray, tile_px: int, goal: int,
                  cancelled: Cancelled | None = None) -> float:
    """モザイク out（その場で書き換え）を、写真を変形してメイン画像 target に近づける。

    goal：全体表示での近さの目標（%）。戻り値：仕上がりの近さ（%）。
    """
    raw = out.copy()   # 変形前（目標より近づきすぎたときに、変形を弱めるため）
    H = out.shape[0] - out.shape[0] % tile_px
    for y in range(0, H, tile_px):
        _check(cancelled)
        a = _to_tiles(out[y:y + tile_px], tile_px)
        b = _to_tiles(target[y:y + tile_px], tile_px)
        out[y:y + tile_px, :a.shape[0] * tile_px] = _from_tiles(fit_tiles_to_target(a, b)).round().astype(np.uint8)
    # 足りない分は、メイン画像を必要なだけ重ねる（重ねる量は計算で決まる：近さは重ねる量に比例して上がる）
    pa, pb = _preview_arrays(out, target)
    err = float(np.abs(pa - pb).mean() / 255.0)
    need = 1.0 - goal / 100.0
    if err < need:
        # 変形だけで目標より近くなった → 変形を弱めて、ちょうど目標の近さにする（写真の模様がよく見える）
        pr, _ = _preview_arrays(raw, target)
        lo, hi = 0.0, 1.0       # 変形の強さ（全体表示は強さに比例して変わるので、縮小画像で計算できる）
        for _ in range(20):
            mid = (lo + hi) / 2
            e = float(np.abs(pr + (pa - pr) * mid - pb).mean() / 255.0)
            lo, hi = (lo, mid) if e <= need else (mid, hi)
        gamma = hi
        strip = 256
        for y in range(0, out.shape[0], strip):
            _check(cancelled)
            r0 = raw[y:y + strip].astype(np.float32)
            out[y:y + strip] = np.clip((r0 + (out[y:y + strip] - r0) * gamma).round(), 0, 255).astype(np.uint8)
        del raw
        return similarity(out, target)
    del raw
    if err > need > 0 or (goal >= 100 and err > 0):
        beta = 1.0 - need / err
        strip = 256
        for y in range(0, out.shape[0], strip):
            _check(cancelled)
            seg = out[y:y + strip].astype(np.float32)
            seg += (target[y:y + strip].astype(np.float32) - seg) * beta
            out[y:y + strip] = np.clip(seg.round(), 0, 255).astype(np.uint8)
    return similarity(out, target)


def _draw_borders(out: np.ndarray, cols: int, rows: int, tile_px: int, color=(20, 18, 28)) -> None:
    bw = max(1, tile_px // 25)
    for r in range(rows):
        out[r * tile_px:r * tile_px + bw, :] = color
    for c in range(cols):
        out[:, c * tile_px:c * tile_px + bw] = color


def _make_preview(img: Image.Image) -> Image.Image:
    p = img.copy()
    p.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE), Image.Resampling.LANCZOS)
    return p


# --------------------------------------------------------------------------
# 写真モザイク
# --------------------------------------------------------------------------
def generate_photo_mosaic(main_photo_path: str, records: list[PhotoRecord], s: PhotoMosaicSettings,
                          progress: Progress | None = None, is_cancelled: Cancelled | None = None) -> MosaicResult:
    t0 = time.time()
    if not records:
        raise MosaicError("タイル用の写真が登録されていません。", "① 写真コレクションに写真を追加してください。")
    W, H = s.cols * s.tile_px, s.rows * s.tile_px
    check_output_size(W, H)
    TOTAL = 1000
    report = progress or (lambda d, t: None)
    warnings: list[str] = []

    main = image_io.load_image(main_photo_path, max_side=max(W, H, s.cols * 2, s.rows * 2))
    cell_feats, cell_rgb = _cell_features(main.image, s.cols, s.rows)
    tile_feats = np.array([r.grid_lab for r in records], dtype=np.float32)
    n_cells = s.cols * s.rows
    max_uses = _effective_max_uses(n_cells, len(records), s.max_uses)
    if _required_uses(n_cells, len(records)) > s.max_uses:
        warnings.append(f"写真の枚数（{len(records):,}枚）に対してタイルが多いため、"
                        f"同じ写真の使用回数を{s.max_uses}回から最大{max_uses}回に増やしました。"
                        "写真を増やすと、より多彩なモザイクになります。")
    rng = np.random.default_rng(s.seed)
    assign = assign_tiles(cell_feats, tile_feats, s.cols, s.rows, max_uses, rng,
                          progress=lambda d, t: report(int(200 * d / max(1, t)), TOTAL), cancelled=is_cancelled)

    used = sorted(set(assign.tolist()))
    tiles, failed = _load_tiles(records, used, s.tile_px, report, is_cancelled, 200, 400, TOTAL)
    if failed:
        warnings.append(f"{len(failed)}枚の写真が読み込めなかったため、その部分は元の色で塗りました（写真が移動・削除された可能性）。")

    fit_on = s.fit > 0
    alpha = 0.0 if fit_on else 0.55 * (s.color_match / 100) ** 1.5   # 変形するときは、色寄せは変形に含まれる
    tint_alpha = np.full(n_cells, alpha, dtype=np.float32)
    out = _compose(assign, s.cols, s.rows, s.tile_px, tiles, cell_rgb, tint_alpha, cell_rgb,
                   report, is_cancelled, 600, 250 if fit_on else 300, TOTAL)
    del tiles

    overlay = None
    if s.edge > 0 or fit_on:
        overlay = np.asarray(ImageOps.fit(main.image, (W, H), Image.Resampling.LANCZOS), dtype=np.uint8)
    if s.edge > 0:  # 元の写真を重ねて輪郭をはっきりさせる
        ea = 0.4 * s.edge / 100
        strip = 256
        for y in range(0, H, strip):
            _check(is_cancelled)
            seg = out[y:y + strip].astype(np.float32)
            seg = seg * (1 - ea) + overlay[y:y + strip].astype(np.float32) * ea
            out[y:y + strip] = seg.astype(np.uint8)
    if fit_on:  # 写真を1枚ずつ変形して、メイン画像に近づける
        report(860, TOTAL)
        closeness = fit_to_target(out, overlay, s.tile_px, s.fit, is_cancelled)
    elif overlay is not None:
        closeness = similarity(out, overlay)
    else:
        closeness = similarity(out, np.asarray(ImageOps.fit(main.image, (W, H), Image.Resampling.LANCZOS)))
    del overlay
    if s.border:
        _draw_borders(out, s.cols, s.rows, s.tile_px)
    report(980, TOTAL)

    img = Image.fromarray(out, "RGB")
    uses = np.bincount(assign, minlength=len(records))
    stats = {"出力サイズ": f"{W:,}×{H:,}px", "タイル数": f"{n_cells:,}枚",
             "使った写真": f"{len(used):,}種類", "最大使用回数": f"{int(uses.max())}回",
             "メイン画像との近さ": f"{closeness:.1f}%（全体表示で比較）",
             "生成時間": f"{time.time() - t0:.1f}秒"}
    settings = {"mode": "photo", "main_photo": main_photo_path, **s.__dict__,
                "effective_max_uses": max_uses, "tiles": [records[i].path for i in used]}
    report(TOTAL, TOTAL)
    return MosaicResult(img, _make_preview(img), stats, warnings, settings)


# --------------------------------------------------------------------------
# 文字モザイク
# --------------------------------------------------------------------------
@dataclass
class TextLayout:
    """文字モザイクの配置（どの升目にどの写真を、どの色寄せで置くか）。静止画とアニメーションで共通。"""
    cols: int
    rows: int
    assign: np.ndarray        # 写真の番号（-1 は単色）
    is_text: np.ndarray       # 文字の升目か
    tint_rgb: np.ndarray      # 寄せる色
    tint_alpha: np.ndarray    # 寄せる強さ
    solid: np.ndarray         # 写真がないときの色
    used_kinds: int
    mask_result: object
    warnings: list[str]


def build_text_layout(text: str, mask_settings: text_mask.TextMaskSettings, records: list[PhotoRecord],
                      s: TextMosaicSettings) -> TextLayout:
    if not records:
        raise MosaicError("タイル用の写真が登録されていません。", "① 写真コレクションに写真を追加してください。")
    res = text_mask.render_text_mask(text, mask_settings)
    cov = text_mask.mask_coverage(res.mask, s.cols, s.rows).reshape(-1)
    is_text = cov >= s.threshold
    text_cells = np.where(is_text)[0]
    bg_cells = np.where(~is_text)[0]
    if len(text_cells) == 0:
        raise MosaicError("文字の部分にタイルがありません。", "タイルの数を増やすか、文字を太くしてください。")
    bg_lab = srgb_to_lab(np.array(s.bg_color, dtype=np.float64))
    rng = np.random.default_rng(s.seed)
    assign, used_kinds = assign_text_tiles(text_cells, bg_cells, records, s.cols, s.rows,
                                           float(bg_lab[0]), s.bg_photos, rng)
    n_cells = s.cols * s.rows
    text_rgb = np.array(s.text_color, dtype=np.float32)
    bg_rgb = np.array(s.bg_color, dtype=np.float32)
    tint_rgb = np.where(is_text[:, None], text_rgb[None, :], bg_rgb[None, :]).astype(np.float32)
    solid = tint_rgb.copy()
    # 読みやすさの補助：背景との明るさの差が小さい写真ほど、文字色に強く寄せる
    tile_l = np.array([r.mean_lab[0] for r in records], dtype=np.float32)
    tint_alpha = np.zeros(n_cells, dtype=np.float32)
    for i in text_cells:
        diff = abs(float(tile_l[assign[i]]) - float(bg_lab[0]))
        tint_alpha[i] = 0.12 if diff >= 40 else 0.12 + (40 - diff) / 40 * 0.38
    if s.bg_photos:
        tint_alpha[bg_cells] = 0.72  # 背景の写真は背景色に強く寄せて、文字を目立たせる
    return TextLayout(s.cols, s.rows, assign, is_text, tint_rgb, tint_alpha, solid, used_kinds, res,
                      list(res.warnings))


def generate_text_mosaic(text: str, mask_settings: text_mask.TextMaskSettings, records: list[PhotoRecord],
                         s: TextMosaicSettings, progress: Progress | None = None,
                         is_cancelled: Cancelled | None = None) -> MosaicResult:
    t0 = time.time()
    W, H = s.cols * s.tile_px, s.rows * s.tile_px
    check_output_size(W, H)
    TOTAL = 1000
    report = progress or (lambda d, t: None)
    lay = build_text_layout(text, mask_settings, records, s)
    res, assign, warnings = lay.mask_result, lay.assign, list(lay.warnings)
    text_cells = np.where(lay.is_text)[0]
    used_kinds, n_cells = lay.used_kinds, s.cols * s.rows
    report(200, TOTAL)

    used = sorted(set(assign[assign >= 0].tolist()))
    tiles, failed = _load_tiles(records, used, s.tile_px, report, is_cancelled, 200, 400, TOTAL)
    if failed:
        warnings.append(f"{len(failed)}枚の写真が読み込めなかったため、その部分は文字色で塗りました。")
    out = _compose(assign, s.cols, s.rows, s.tile_px, tiles, lay.tint_rgb, lay.tint_alpha, lay.solid,
                   report, is_cancelled, 600, 350, TOTAL)
    del tiles
    report(980, TOTAL)

    img = Image.fromarray(out, "RGB")
    stats = {"出力サイズ": f"{W:,}×{H:,}px", "タイル数": f"{n_cells:,}枚",
             "文字のタイル": f"{len(text_cells):,}枚", "使った写真": f"{used_kinds:,}種類",
             "文字の配置": " ／ ".join(res.lines), "生成時間": f"{time.time() - t0:.1f}秒"}
    ms = mask_settings
    settings = {"mode": "text", "text": res.text, "lines": res.lines,
                "font": res.font.display_name, "font_path": res.font.path, "font_index": res.font.index,
                "font_size": res.font_size, "weight": ms.weight, "letter_spacing": ms.letter_spacing,
                "line_spacing": ms.line_spacing, "multiline": ms.multiline, "canvas_size": list(ms.canvas_size),
                **{k: (list(v) if isinstance(v, tuple) else v) for k, v in s.__dict__.items()},
                "tiles": [records[i].path for i in used]}
    report(TOTAL, TOTAL)
    return MosaicResult(img, _make_preview(img), stats, warnings, settings)
