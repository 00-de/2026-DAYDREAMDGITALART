"""集合アニメーション（200種類）と動画の書き出しのテスト。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import animation as an  # noqa: E402
from app import collection as col  # noqa: E402
from app import text_mask as tm  # noqa: E402
from app import video_export as ve  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv[:1])


def frame_array(img):
    return np.asarray(ve.qimage_to_pil(img), dtype=float)


class PresetTest(unittest.TestCase):
    def test_200_unique_presets(self):
        self.assertEqual(len(an.PRESETS), 200)
        self.assertEqual(len({p.name for p in an.PRESETS}), 200)
        self.assertEqual(len({(p.origin, p.order, p.path) for p in an.PRESETS}), 200)
        self.assertTrue(an.PRESETS[0].name.startswith("001"))
        self.assertTrue(an.PRESETS[199].name.startswith("200"))

    def test_presets_are_deterministic(self):
        self.assertEqual(an._build_presets(), an.PRESETS)


@unittest.skipUnless(tm.find_japanese_fonts(), "日本語フォントがありません")
class RenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(cls.tmp.name)
        tiles = root / "タイル"
        tiles.mkdir()
        rng = np.random.default_rng(3)
        for i in range(40):  # 明るい写真40枚
            c = tuple(int(v) for v in rng.integers(150, 256, 3))
            Image.new("RGB", (60, 40), c).save(tiles / f"t{i}.jpg", "JPEG", quality=95)
        cache = col.AnalysisCache(root / "c.sqlite")
        cls.records = col.analyze_sources([str(tiles)], cache=cache).records
        cache.close()
        cls.st = an.AnimationSettings(width=180, height=320, fps=10, duration=2, hold=0.5, preset=0, seed=1)
        cls.r = an.build_renderer("ドリプラ", tm.TextMaskSettings(), cls.records, (255, 255, 255),
                                  (10, 10, 30), False, cls.st)
        cls.out = root / "出力"
        cls.out.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_frame_count(self):
        self.assertEqual(self.r.frame_count, 20 + 5)

    def test_last_frame_forms_text(self):
        """最後のコマで、文字の升目が明るく（写真）、背景が暗い＝文字ができあがっている。"""
        last = frame_array(self.r.frame(self.r.frame_count - 1)).mean(axis=2)
        lay = self.r.layout
        ox, oy, T = self.r.offset[0], self.r.offset[1], self.r.tile
        cell = np.array([[last[oy + rr * T + T // 2, ox + cc * T + T // 2]
                          for cc in range(lay.cols)] for rr in range(lay.rows)]).reshape(-1)
        self.assertGreater(cell[lay.is_text].mean(), 120)
        self.assertLess(cell[~lay.is_text].mean(), 40)

    def test_every_origin_and_path_animates_and_lands(self):
        """10種類の出発位置・4種類の動き方すべてで、途中は最後と違い、最後は同じ形に着地する。"""
        final = None
        checked = set()
        for p in an.PRESETS:
            key = (p.origin, p.path)
            if key in checked:
                continue
            checked.add(key)
            r = self.r.with_settings(an.AnimationSettings(**{**self.st.__dict__, "preset": p.index}))
            last = frame_array(r.frame(r.frame_count - 1))
            mid = frame_array(r.frame(max(1, r.n_anim // 8)))  # 動きの序盤（速い種類でもまだ動いている時点）
            if final is None:
                final = last
            self.assertLess(np.abs(last - final).mean(), 1.0, f"{p.name}：最後の形が違う")
            self.assertGreater(np.abs(mid - last).mean(), 1.0, f"{p.name}：動いていない")
        self.assertEqual(len(checked), 40)

    def test_tile_levels_make_tiles_bigger(self):
        """「写真の大きさ」を上げるほど、1マス（写真1枚）が大きくなる。"""
        sizes = []
        for lv in range(4):
            st = an.AnimationSettings(**{**self.st.__dict__, "width": 540, "height": 960, "tile_level": lv})
            r = an.build_renderer("DayDream Plus", tm.TextMaskSettings(), self.records, (255, 255, 255),
                                  (10, 10, 30), False, st)
            sizes.append(r.tile)
            self.assertEqual(r.offset[0] * 2 + r.layout.cols * r.tile, 540 - (540 - r.layout.cols * r.tile) % 2)
        self.assertEqual(sizes, sorted(sizes))
        self.assertGreater(sizes[3], sizes[0] * 1.8, sizes)

    def test_landed_tiles_are_pixel_exact(self):
        """着地した写真は、読み込んだ写真そのもの（にじみなし）で描かれる。"""
        r = self.r
        last = frame_array(r.frame(r.frame_count - 1))
        ox, oy, T = r.offset[0], r.offset[1], r.tile
        lay = r.layout
        i = int(np.where(lay.is_text)[0][0])
        rr, cc = divmod(i, lay.cols)
        got = last[oy + rr * T: oy + rr * T + T, ox + cc * T: ox + cc * T + T]
        ti, tint = r.items[0]
        exp = np.asarray(ve.qimage_to_pil(r.exact[ti]), dtype=float)
        if tint is not None:
            a = tint.alpha() / 255
            exp = exp * (1 - a) + np.array([tint.red(), tint.green(), tint.blue()]) * a
        self.assertLess(np.abs(got - exp).mean(), 2.0)

    def test_flying_tiles_are_bigger(self):
        """飛んでくる途中の写真は、着地したときより大きい（fly_scale）。"""
        st = an.AnimationSettings(**{**self.st.__dict__, "preset": 0, "fly_scale": 3.0})
        r = self.r.with_settings(st)
        p, _, _, scale, _ = r._state(0.3)
        flying = (p > 0.05) & (p < 0.6)
        self.assertTrue(flying.any())
        base = an.PRESETS[0].start_scale
        self.assertGreater(float(np.median(scale[flying])), max(1.0, base) * 1.4)
        _, _, _, scale_end, _ = r._state(1.0)
        self.assertAlmostEqual(float(np.abs(scale_end - 1).max()), 0.0, places=3)

    def test_export_gif(self):
        path = ve.export_gif(self.r, str(self.out / "アニメ"))
        with Image.open(path) as im:
            self.assertEqual(im.format, "GIF")
            self.assertGreater(im.n_frames, 5)

    def test_export_png_sequence(self):
        folder, n = ve.export_png_sequence(self.r, str(self.out / "連番"))
        self.assertEqual(n, self.r.frame_count)
        self.assertEqual(len(list(Path(folder).glob("frame_*.png"))), n)

    def test_export_mp4(self):
        if not ve.mp4_codec_name():
            self.skipTest("この環境では MP4 を作れません")
        path, codec = ve.export_mp4(self.r, str(self.out / "動画"))
        self.assertTrue(Path(path).exists())
        self.assertGreater(Path(path).stat().st_size, 1000)
        self.assertIn(codec, ("H.264", "MPEG-4"))
        self.assertFalse(list(self.out.glob(".saving_*")), "一時ファイルが残っている")

    def test_cancel(self):
        with self.assertRaises(ve.ExportCancelled):
            ve.export_png_sequence(self.r, str(self.out / "中止"), is_cancelled=lambda: True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
