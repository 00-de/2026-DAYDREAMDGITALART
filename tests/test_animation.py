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
            r = self.r.with_settings(an.AnimationSettings(**{**self.st.__dict__, "preset": p.index}))  # noqa
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

    def test_flying_photos_are_150px_wide_with_aspect_ratio(self):
        """飛んでくる途中の写真は横幅 150px・元の縦横比のまま。着地すると1マスの正方形。"""
        st = an.AnimationSettings(**{**self.st.__dict__, "width": 1080, "height": 1920, "preset": 0,
                                     "fly_width": 150})
        r = an.build_renderer("ドリプラ", tm.TextMaskSettings(), self.records, (255, 255, 255),
                              (10, 10, 30), False, st)
        f = next(iter(r.fly.values()))
        self.assertAlmostEqual(f.h0 / f.w0, 40 / 60, delta=0.02)    # テスト写真は 60×40（横長）
        self.assertEqual(f.w0, min(150, 60))  # 150px で読み込む（元の写真より大きくは引き伸ばさない）
        p, _, _, width, mix, _ = r._state(0.0)
        start = p <= 0.0
        self.assertTrue(np.allclose(width[start], 150 * max(1.0, an.PRESETS[0].start_scale)))
        self.assertTrue(np.allclose(mix[start], 1.0))               # 出発時は写真全体（切り取りなし）
        _, _, _, width_end, mix_end, _ = r._state(1.0)
        self.assertTrue(np.allclose(width_end, r.tile, atol=0.01))
        self.assertTrue(np.allclose(mix_end, 0.0))
        # 飛行中のコマで、150px 前後の写真が実際に描かれている（文字の部分以外にも色がある）
        mid = frame_array(r.frame(2))
        self.assertGreater((np.abs(mid - [10, 10, 30]).sum(axis=2) > 30).mean(), 0.02)

    def test_multi_level_source_is_used_when_shrinking(self):
        f = next(iter(self.r.fly.values()))
        k_big, _ = f.pick(f.w0, f.w0)
        k_small, _ = f.pick(f.w0 / 4, f.w0)
        self.assertEqual(k_big, 1.0)
        self.assertLessEqual(k_small, 0.5)

    def test_photo_mosaic_animation_reproduces_reference(self):
        """写真モザイク：最後のコマで参考画像の色の配置（左が赤・右が青）が再現される。"""
        ref = Path(self.tmp.name) / "参考.png"
        im = Image.new("RGB", (400, 300), (230, 30, 30))
        im.paste((30, 30, 230), (200, 0, 400, 300))
        im.save(ref)
        rng = np.random.default_rng(4)
        tdir = Path(self.tmp.name) / "色タイル"
        tdir.mkdir(exist_ok=True)
        for i in range(40):
            c = tuple(int(v) for v in rng.integers(0, 256, 3))
            Image.new("RGB", (80, 60), c).save(tdir / f"c{i}.png")
        for i, c in enumerate([(240, 20, 20), (220, 40, 30), (20, 20, 240), (40, 30, 220)]):
            Image.new("RGB", (80, 60), c).save(tdir / f"rb{i}.png")
        cache = col.AnalysisCache(Path(self.tmp.name) / "c2.sqlite")
        recs = col.analyze_sources([str(tdir)], cache=cache).records
        cache.close()
        st = an.AnimationSettings(width=360, height=640, fps=10, duration=1, hold=0.2, preset=7, seed=1,
                                  tile_level=1, fly_width=60)
        r = an.build_photo_renderer(str(ref), recs, st, max_uses=50, color_match=0, edge=0)
        self.assertGreater(r.layout.cols, r.layout.rows)   # 横長の参考画像は横長のまま収まる
        last = frame_array(r.frame(r.frame_count - 1))
        ox, oy, T = r.offset[0], r.offset[1], r.tile
        gw, gh = r.layout.cols * T, r.layout.rows * T
        left = last[oy:oy + gh, ox:ox + gw // 2].mean(axis=(0, 1))
        right = last[oy:oy + gh, ox + gw // 2:ox + gw].mean(axis=(0, 1))
        self.assertGreater(left[0], left[2] + 40)
        self.assertGreater(right[2], right[0] + 40)
        mid = frame_array(r.frame(2))
        self.assertGreater(np.abs(mid - last).mean(), 1.0)   # 途中は動いている

    def test_photo_animation_fit_99_percent(self):
        """写真モザイクの動画：変形した写真が着地し、最後のコマがメイン画像に 99% 近い。途中は動いている。"""
        from PIL import ImageOps

        from app import mosaic_engine as me
        root = Path(self.tmp.name)
        ref = root / "参考_fit.png"
        g = np.zeros((400, 600, 3), "uint8")
        g[..., 0] = np.linspace(0, 255, 600)
        g[..., 2] = np.linspace(255, 0, 400)[:, None]
        g[150:250, 200:400] = (250, 230, 40)
        Image.fromarray(g).save(ref)
        st = an.AnimationSettings(width=480, height=270, fps=10, duration=1, hold=0.2, preset=3, seed=1,
                                  tile_level=1, fly_width=60)
        r = an.build_photo_renderer(str(ref), self.records, st, 5, 70, 20, fit=99)
        self.assertGreaterEqual(r.closeness, 98.95)
        last = ve.qimage_to_pil(r.frame(r.frame_count - 1))
        ox, oy, T = r.offset[0], r.offset[1], r.tile
        grid = last.crop((ox, oy, ox + r.layout.cols * T, oy + r.layout.rows * T))
        tgt = ImageOps.fit(Image.open(ref).convert("RGB"), grid.size, Image.Resampling.LANCZOS)
        self.assertGreaterEqual(me.similarity(np.asarray(grid), np.asarray(tgt)), 98.9)
        mid = frame_array(r.frame(2))
        self.assertGreater(np.abs(mid - np.asarray(last, dtype=float)).mean(), 1.0)
        r2 = r.with_settings(an.AnimationSettings(**{**st.__dict__, "preset": 50}))
        self.assertLess(np.abs(frame_array(r2.frame(r2.frame_count - 1)) - np.asarray(last, dtype=float)).mean(), 1.0)

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

    def test_bitrate_matches_700_to_850kb_per_frame(self):
        """高画質の目安（1920×1080・1コマ 約780KB）→ ビットレートの計算が合っている。小さい動画は面積に合わせて減る。"""
        self.assertEqual(ve.mp4_bitrate(1920, 1080, 30, 780), 780 * 1024 * 8 * 30)
        self.assertEqual(ve.mp4_bitrate(1080, 1920, 30, 780), 780 * 1024 * 8 * 30)   # 縦長も同じ
        self.assertEqual(ve.mp4_bitrate(960, 540, 30, 780), 780 * 1024 * 8 * 30 // 4)
        self.assertEqual(ve.DEFAULT_KB_PER_FRAME, 780)
        self.assertEqual(ve.MP4_QUALITIES[0][2], 780)

    def test_export_mp4_full_hd_is_700_to_850kb_per_frame(self):
        """1920×1080 の MP4 が、1コマあたり 700〜850KB の大きさで書き出される（写真モザイク）。"""
        if ve.mp4_codec_name() != "MPEG-4":
            self.skipTest("実際の大きさの確認は MPEG-4 の環境で行う（Windows の H.264 は書き出し後の画面で表示）")
        root = Path(self.tmp.name)
        tdir = root / "細かい写真"
        tdir.mkdir(exist_ok=True)
        rng = np.random.default_rng(9)
        for i in range(60):  # 本物の写真のように細かい模様のある写真
            a = (rng.random((30, 40, 3)) * 255).astype("uint8")
            Image.fromarray(a).resize((400, 300), Image.Resampling.BICUBIC).save(tdir / f"p{i}.jpg", quality=90)
        ref = root / "参考_グラデ.png"
        g = np.zeros((600, 900, 3), "uint8")
        g[..., 0] = np.linspace(0, 255, 900)
        g[..., 1] = np.linspace(0, 255, 600)[:, None]
        Image.fromarray(g).save(ref)
        cache = col.AnalysisCache(root / "c3.sqlite")
        recs = col.analyze_sources([str(tdir)], cache=cache).records
        cache.close()
        st = an.AnimationSettings(width=1920, height=1080, fps=30, duration=1.5, hold=0.5, preset=0, seed=1,
                                  tile_level=1, fly_width=100)
        r = an.build_photo_renderer(str(ref), recs, st, 5, 70, 20)
        path, _ = ve.export_mp4(r, str(self.out / "高画質"), kb_per_frame=780)
        per = Path(path).stat().st_size / r.frame_count / 1024
        self.assertGreaterEqual(per, 700, f"1コマ {per:.0f}KB")
        self.assertLessEqual(per, 850, f"1コマ {per:.0f}KB")

    def test_cancel(self):
        with self.assertRaises(ve.ExportCancelled):
            ve.export_png_sequence(self.r, str(self.out / "中止"), is_cancelled=lambda: True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
