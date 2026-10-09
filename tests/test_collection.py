"""写真コレクション（フェーズ3）のテスト。"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import collection as col  # noqa: E402
from app.color import srgb_to_lab  # noqa: E402


class ColorTest(unittest.TestCase):
    def test_lab_reference_values(self):
        lab = srgb_to_lab(np.array([[255, 255, 255], [0, 0, 0], [255, 0, 0]]))
        np.testing.assert_allclose(lab[0], [100, 0, 0], atol=0.1)
        np.testing.assert_allclose(lab[1], [0, 0, 0], atol=0.1)
        np.testing.assert_allclose(lab[2], [53.24, 80.09, 67.20], atol=0.2)  # 赤の基準値


class CollectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.dir = root / "ライブ写真"
        sub = self.dir / "サブフォルダー"
        sub.mkdir(parents=True)
        colors = [(220, 30, 40), (30, 200, 60), (40, 60, 230), (240, 220, 40)]
        for i, c in enumerate(colors):
            Image.new("RGB", (300, 200), c).save(self.dir / f"写真{i}.jpg", "JPEG", quality=90)
        Image.new("RGB", (300, 200), (10, 10, 10)).save(self.dir / "黒.jfif", "JPEG")
        Image.new("RGBA", (300, 200), (0, 0, 0, 0)).save(self.dir / "透明.png")
        shutil.copy(self.dir / "写真0.jpg", self.dir / "写真0のコピー.jpg")  # 名前違い・中身同じ
        (self.dir / "壊れた.jpg").write_bytes(b"\xff\xd8\xff\xe0 broken data")
        (self.dir / "メモ.txt").write_text("対象外", encoding="utf-8")
        Image.new("RGB", (100, 100), (128, 0, 128)).save(sub / "紫.png")
        self.cache = col.AnalysisCache(root / "cache.sqlite")

    def tearDown(self):
        self.cache.close()
        self.tmp.cleanup()

    def test_folder_analysis(self):
        r = col.analyze_sources([str(self.dir)], cache=self.cache)
        self.assertEqual(len(r.records), 6)              # 4色＋黒(JFIF)＋透明PNG
        self.assertEqual(len(r.duplicates), 1)           # 写真0のコピー
        self.assertEqual(len(r.failed), 1)               # 壊れた.jpg
        self.assertIn("壊れた.jpg", r.failed[0][0])
        red = next(x for x in r.records if x.path.endswith("写真0.jpg") or x.path.endswith("写真0のコピー.jpg"))
        self.assertGreater(red.mean_lab[1], 40)          # 赤（a値が大きい）
        self.assertEqual((red.width, red.height), (300, 200))
        self.assertEqual(len(red.grid_lab), 12)
        thumb = Image.open(__import__("io").BytesIO(red.thumb_jpeg))
        self.assertLessEqual(max(thumb.size), col.THUMB_SIDE)
        transparent = next(x for x in r.records if x.path.endswith("透明.png"))
        self.assertGreater(transparent.mean_lab[0], 95)  # 透明部分は白で合成される

    def test_recursive(self):
        r = col.analyze_sources([str(self.dir)], recursive=True, cache=self.cache)
        self.assertEqual(len(r.records), 7)

    def test_individual_files_and_ignored(self):
        files = [str(self.dir / "写真1.jpg"), str(self.dir / "写真2.jpg"), str(self.dir / "メモ.txt")]
        r = col.analyze_sources(files, cache=self.cache)
        self.assertEqual(len(r.records), 2)
        self.assertEqual(r.ignored, 1)

    def test_cache_reuse(self):
        first = col.analyze_sources([str(self.dir)], cache=self.cache)
        self.assertEqual(first.from_cache, 0)
        second = col.analyze_sources([str(self.dir)], cache=self.cache)
        self.assertEqual(second.from_cache, len(first.records) + len(first.duplicates))
        self.assertEqual([x.sha1 for x in second.records], [x.sha1 for x in first.records])

    def test_cache_invalidated_when_file_changes(self):
        col.analyze_sources([str(self.dir)], cache=self.cache)
        p = self.dir / "写真1.jpg"
        Image.new("RGB", (300, 200), (0, 0, 0)).save(p, "JPEG")
        import os
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        r = col.analyze_sources([str(p)], cache=self.cache)
        self.assertEqual(r.from_cache, 0)
        self.assertLess(r.records[0].mean_lab[0], 5)     # 黒に変わったことを反映

    def test_capacity_limit(self):
        r = col.analyze_sources([str(self.dir)], capacity=3, cache=self.cache)
        self.assertEqual(len(r.records), 3)
        self.assertGreaterEqual(r.over_limit, 3)

    def test_cancel(self):
        calls = {"n": 0}

        def cancelled():
            calls["n"] += 1
            return calls["n"] > 2
        r = col.analyze_sources([str(self.dir)], is_cancelled=cancelled, cache=self.cache, workers=1)
        self.assertTrue(r.cancelled)
        self.assertLess(len(r.records), 6)

    def test_merge_skips_known(self):
        c = col.PhotoCollection()
        r1 = col.analyze_sources([str(self.dir / "写真1.jpg")], cache=self.cache)
        c.merge(r1, [str(self.dir / "写真1.jpg")])
        r2 = col.analyze_sources([str(self.dir)], known_paths=c.known_paths(), known_sha1=c.known_sha1(),
                                 capacity=c.capacity, cache=self.cache)
        c.merge(r2, [str(self.dir)])
        self.assertEqual(len(c), 6)
        self.assertEqual(c.lab_matrix().shape, (6, 3))
        c.clear()
        self.assertEqual(len(c), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
