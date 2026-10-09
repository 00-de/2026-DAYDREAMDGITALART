"""色の計算。

Lab色空間（ラボ）とは：人の目が感じる「色の違い」に近い形で色を表す物差しです。
RGB の数字の差よりも、Lab の差のほうが「見た目の似ている度合い」に近くなるため、
タイル写真選びに使います（仕様書 7章）。
"""

from __future__ import annotations

import numpy as np

# sRGB → XYZ（D65）の変換行列
_M = np.array([[0.4124564, 0.3575761, 0.1804375],
               [0.2126729, 0.7151522, 0.0721750],
               [0.0193339, 0.1191920, 0.9503041]], dtype=np.float64)
_WHITE = np.array([0.95047, 1.0, 1.08883])


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """0〜255 の RGB（形 [..., 3]）を Lab（L:0〜100, a/b:おおむね-128〜127）に変換する。"""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = lin @ _M.T / _WHITE
    eps, kappa = 216 / 24389, 24389 / 27
    f = np.where(xyz > eps, np.cbrt(xyz), (kappa * xyz + 16) / 116)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def image_features(img_rgb: np.ndarray, grid: int = 2) -> tuple[list[float], list[float], list[float]]:
    """縮小済みの画像（numpy配列 高さ×幅×3）から色の特徴を計算する。

    戻り値：(平均RGB, 平均Lab, grid×grid に分けた各区画の平均Lab を並べたもの)
    区画ごとの色を持っておくと、「上が空色・下が緑」のような写真の色の配置も考慮できる。
    """
    arr = np.asarray(img_rgb, dtype=np.float64)
    mean_rgb = arr.reshape(-1, 3).mean(axis=0)
    lab = srgb_to_lab(arr)
    mean_lab = lab.reshape(-1, 3).mean(axis=0)
    h, w = lab.shape[:2]
    cells = []
    for gy in range(grid):
        for gx in range(grid):
            part = lab[gy * h // grid:(gy + 1) * h // grid, gx * w // grid:(gx + 1) * w // grid]
            cells.extend(part.reshape(-1, 3).mean(axis=0).tolist())
    return mean_rgb.tolist(), mean_lab.tolist(), cells
