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
        cell = np.array([[last[int(rr * self.r.cell_h + self.r.cell_h / 2), int(cc * self.r.cell_w + self.r.cell_w / 2)]
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
            mid = frame_array(r.frame(r.n_anim // 3))
            if final is None:
                final = last
            self.assertLess(np.abs(last - final).mean(), 1.0, f"{p.name}：最後の形が違う")
            self.assertGreater(np.abs(mid - last).mean(), 1.0, f"{p.name}：動いていない")
        self.assertEqual(len(checked), 40)

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
