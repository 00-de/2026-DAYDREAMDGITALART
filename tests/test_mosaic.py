"""モザイク生成と保存（フェーズ6・8）のテスト。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import collection as col  # noqa: E402
from app import exporter, mosaic_engine as me, text_mask as tm  # noqa: E402
from app.errors import MosaicError  # noqa: E402


class MosaicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        tiles = root / "タイル"
        tiles.mkdir()
        rng = np.random.default_rng(1)
        for i in range(60):  # 色の違う写真60枚（白・黒・赤・青なども含む）
            c = tuple(int(v) for v in rng.integers(0, 256, 3))
            Image.new("RGB", (80, 60), c).save(tiles / f"t{i:02d}.jpg", "JPEG", quality=95)
        for name, c in (("white", (250, 250, 250)), ("black", (5, 5, 5)), ("red", (230, 20, 20)), ("blue", (20, 20, 230))):
            Image.new("RGB", (80, 60), c).save(tiles / f"{name}.png")
        cls.records = col.analyze_sources([str(tiles)], cache=col.AnalysisCache(root / "c.sqlite")).records
        # メイン写真：左半分が赤、右半分が青
        main = Image.new("RGB", (200, 100), (230, 20, 20))
        main.paste((20, 20, 230), (100, 0, 200, 100))
        cls.main = root / "メイン写真.jfif"
        main.save(cls.main, "JPEG", quality=95)
        cls.out_dir = root / "出力"
        cls.out_dir.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_photo_mosaic_reproduces_colors(self):
        s = me.PhotoMosaicSettings(cols=20, rows=10, tile_px=12, max_uses=5, color_match=0, edge=0, seed=3)
        r = me.generate_photo_mosaic(str(self.main), self.records, s)
        self.assertEqual(r.image.size, (240, 120))
        arr = np.asarray(r.image, dtype=float)
        left, right = arr[:, :120].mean(axis=(0, 1)), arr[:, 120:].mean(axis=(0, 1))
        self.assertGreater(left[0], left[2])      # 左は赤っぽい
        self.assertGreater(right[2], right[0])    # 右は青っぽい
        self.assertLessEqual(r.preview.width, me.PREVIEW_SIDE)

    def test_usage_limit_and_no_adjacent_repeat(self):
        recs = self.records
        cf = np.random.default_rng(0).normal(50, 20, (30 * 20, 12)).astype(np.float32)
        tf = np.array([x.grid_lab for x in recs], dtype=np.float32)
        a = me.assign_tiles(cf, tf, 30, 20, 12, np.random.default_rng(0))
        self.assertLessEqual(np.bincount(a).max(), 12)
        grid = a.reshape(20, 30)
        self.assertFalse((grid[:, 1:] == grid[:, :-1]).any(), "横に同じ写真が並んでいる")
        self.assertFalse((grid[1:, :] == grid[:-1, :]).any(), "縦に同じ写真が並んでいる")

    def test_too_few_photos_raises_limit_with_warning(self):
        s = me.PhotoMosaicSettings(cols=30, rows=20, tile_px=8, max_uses=2, color_match=50, edge=0)
        r = me.generate_photo_mosaic(str(self.main), self.records[:10], s)
        self.assertTrue(any("使用回数" in w for w in r.warnings))

    def test_same_seed_is_reproducible(self):
        s = me.PhotoMosaicSettings(cols=16, rows=8, tile_px=8, seed=42, edge=0)
        a = me.generate_photo_mosaic(str(self.main), self.records, s)
        b = me.generate_photo_mosaic(str(self.main), self.records, s)
        self.assertEqual(a.image.tobytes(), b.image.tobytes())

    def test_border_and_edge_options_run(self):
        s = me.PhotoMosaicSettings(cols=10, rows=5, tile_px=20, border=True, edge=60)
        r = me.generate_photo_mosaic(str(self.main), self.records, s)
        px = np.asarray(r.image)
        self.assertTrue((px[0, :] == (20, 18, 28)).all())  # 境界線

    def test_cancel(self):
        s = me.PhotoMosaicSettings(cols=20, rows=10, tile_px=8)
        with self.assertRaises(me.GenerationCancelled):
            me.generate_photo_mosaic(str(self.main), self.records, s, is_cancelled=lambda: True)

    def test_no_tiles_error(self):
        with self.assertRaises(MosaicError):
            me.generate_photo_mosaic(str(self.main), [], me.PhotoMosaicSettings())

    def test_too_large_output_rejected(self):
        with self.assertRaises(MosaicError) as cm:
            me.check_output_size(30000, 30000)
        self.assertIn("大きすぎ", cm.exception.cause)

    def test_text_mosaic_is_readable(self):
        if not tm.find_japanese_fonts():
            self.skipTest("日本語フォントがありません")
        ms = tm.TextMaskSettings(canvas_size=(1200, 800))
        res_mask = tm.render_text_mask("ドリプラ", ms)
        cols, rows = tm.grid_size_for(res_mask.mask.size, tm.recommend_tile_px(res_mask))
        s = me.TextMosaicSettings(cols=cols, rows=rows, tile_px=6, bg_color=(10, 10, 20))
        r = me.generate_text_mosaic("ドリプラ", ms, self.records, s)
        arr = np.asarray(r.image.convert("L"), dtype=float)
        cov = tm.mask_coverage(res_mask.mask, cols, rows) >= 0.5
        cell = arr.reshape(rows, 6, cols, 6).mean(axis=(1, 3))
        # 文字の升目は背景より十分に明るい（判読できる）
        self.assertGreater(cell[cov].mean() - cell[~cov].mean(), 60)
        self.assertEqual(r.settings["mode"], "text")

    def test_text_mosaic_photo_background(self):
        if not tm.find_japanese_fonts():
            self.skipTest("日本語フォントがありません")
        ms = tm.TextMaskSettings(canvas_size=(900, 600))
        s = me.TextMosaicSettings(cols=60, rows=40, tile_px=5, bg_photos=True)
        r = me.generate_text_mosaic("DD+", ms, self.records, s)
        self.assertEqual(r.image.size, (300, 200))

    def test_save_png_jpeg_and_settings(self):
        img = Image.new("RGB", (400, 300), (100, 150, 200))
        p = exporter.save_image(img, exporter.SaveOptions(str(self.out_dir / "完成"), "PNG"))
        self.assertTrue(p.path.endswith(".png"))
        self.assertEqual(Image.open(p.path).size, (400, 300))
        j = exporter.save_image(img, exporter.SaveOptions(str(self.out_dir / "完成.jpg"), "JPEG", quality=80,
                                                          scale=0.5, save_settings=True), settings={"seed": 1})
        self.assertEqual(Image.open(j.path).format, "JPEG")
        self.assertEqual((j.width, j.height), (200, 150))
        self.assertEqual(json.loads(Path(j.settings_path).read_text(encoding="utf-8"))["seed"], 1)
        self.assertFalse(list(self.out_dir.glob(".ddp_saving_*")), "一時ファイルが残っている")

    def test_save_refuses_to_overwrite_source_photo(self):
        img = Image.new("RGB", (10, 10))
        src = self.records[0].path
        with self.assertRaises(MosaicError):
            exporter.save_image(img, exporter.SaveOptions(str(Path(src).with_suffix(".png")), "PNG"),
                                protected_paths={str(Path(src).with_suffix(".png"))})
        with self.assertRaises(MosaicError):
            exporter.save_image(img, exporter.SaveOptions(str(self.main), "JPEG"), protected_paths={str(self.main)})

    def test_save_missing_folder(self):
        with self.assertRaises(MosaicError):
            exporter.save_image(Image.new("RGB", (5, 5)), exporter.SaveOptions(str(self.out_dir / "無い" / "a.png")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
