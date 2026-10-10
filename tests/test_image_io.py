"""画像読み込みのテスト（フェーズ1：仕様書 15章のうち読み込み関連）。

実行方法は README.txt を参照。テスト用の画像はその場で自動生成します。
"""

import struct
import zlib
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import image_io  # noqa: E402
from app.errors import ImageLoadError  # noqa: E402


def _jpeg_has_jfif_marker(path: Path) -> bool:
    data = path.read_bytes()[:32]
    return data[:2] == b"\xff\xd8" and b"JFIF" in data


class ImageIOTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = cls.dir = Path(cls.tmp.name) / "テスト写真フォルダー"
        d.mkdir()
        red = Image.new("RGB", (400, 300), (220, 30, 40))
        red.save(d / "sample.jpg", "JPEG", quality=90)
        red.save(d / "sample.jpeg", "JPEG", quality=90)
        red.save(d / "sample.jfif", "JPEG", quality=90)        # 本物のJFIF（APP0にJFIF）
        red.save(d / "大文字拡張子.JPG", "JPEG")
        red.save(d / "ドリプラ_ライブ写真.jpg", "JPEG")         # 日本語ファイル名

        # 透過PNG：左半分が透明、右半分が不透明の青
        rgba = Image.new("RGBA", (200, 100), (0, 0, 255, 255))
        rgba.paste((0, 0, 0, 0), (0, 0, 100, 100))
        rgba.save(d / "transparent.png")

        # EXIF回転情報つき（Orientation=6：右に90°回して表示する写真）
        tall = Image.new("RGB", (300, 200), (0, 200, 0))
        exif = Image.Exif()
        exif[0x0112] = 6
        tall.save(d / "rotated.jpg", "JPEG", exif=exif)

        # 破損画像：JPEGの途中で切れている
        good = (d / "sample.jpg").read_bytes()
        (d / "broken.jpg").write_bytes(good[: len(good) // 3])
        # 画像ではないファイルに .jpg を付けたもの
        (d / "not_image.jpg").write_text("これは画像ではありません", encoding="utf-8")
        # 中身はPNGなのに拡張子が .jpg
        red.save(d / "png_named.jpg", "PNG")
        # 対応外の形式（GIF）
        red.save(d / "anim.gif", "GIF")
        (d / "memo.txt").write_text("対象外", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_jpg_jpeg_load(self):
        for name in ("sample.jpg", "sample.jpeg", "大文字拡張子.JPG"):
            r = image_io.load_image(self.dir / name)
            self.assertEqual(r.format, "JPEG")
            self.assertEqual(r.image.mode, "RGB")
            self.assertEqual(r.image.size, (400, 300))

    def test_jfif_load(self):
        p = self.dir / "sample.jfif"
        self.assertTrue(_jpeg_has_jfif_marker(p), "テスト用ファイルが本物のJFIFになっていない")
        r = image_io.load_image(p)
        self.assertEqual(r.format, "JPEG")
        self.assertEqual(r.warning, "")
        px = r.image.getpixel((200, 150))
        self.assertGreater(px[0], 180)  # 赤が保たれている

    def test_png_alpha_is_composited_on_background(self):
        r = image_io.load_image(self.dir / "transparent.png")
        self.assertEqual(r.image.mode, "RGB")
        self.assertEqual(r.image.getpixel((10, 50)), (255, 255, 255))  # 透明部分→白
        self.assertEqual(r.image.getpixel((150, 50)), (0, 0, 255))     # 不透明部分は青のまま
        r2 = image_io.load_image(self.dir / "transparent.png", alpha_background=(0, 0, 0))
        self.assertEqual(r2.image.getpixel((10, 50)), (0, 0, 0))

    def test_japanese_filename(self):
        r = image_io.load_image(self.dir / "ドリプラ_ライブ写真.jpg")
        self.assertEqual(r.image.size, (400, 300))

    def test_exif_rotation(self):
        r = image_io.load_image(self.dir / "rotated.jpg")
        self.assertEqual(r.image.size, (200, 300))  # 横長300×200 → 縦長200×300

    def test_exif_rotation_with_downscale_keeps_original_size(self):
        r = image_io.load_image(self.dir / "rotated.jpg", max_side=50)
        self.assertEqual(r.original_size, (200, 300))
        self.assertLessEqual(max(r.image.size), 50)
        self.assertGreater(r.image.size[1], r.image.size[0])  # 縦長のまま

    def test_broken_image_raises(self):
        with self.assertRaises(ImageLoadError) as cm:
            image_io.load_image(self.dir / "broken.jpg")
        self.assertIn("壊れて", cm.exception.user_message())

    def test_non_image_raises(self):
        with self.assertRaises(ImageLoadError) as cm:
            image_io.load_image(self.dir / "not_image.jpg")
        self.assertIn("認識できません", cm.exception.cause)

    def test_unsupported_format_raises(self):
        with self.assertRaises(ImageLoadError) as cm:
            image_io.load_image(self.dir / "anim.gif")
        self.assertIn("対応していない", cm.exception.cause)

    def test_extension_mismatch_loads_with_warning(self):
        r = image_io.load_image(self.dir / "png_named.jpg")
        self.assertEqual(r.format, "PNG")
        self.assertIn("一致していません", r.warning)

    def test_missing_file_raises(self):
        with self.assertRaises(ImageLoadError):
            image_io.load_image(self.dir / "存在しない.jpg")

    def test_max_side_downscale(self):
        r = image_io.load_image(self.dir / "sample.jpg", max_side=64)
        self.assertLessEqual(max(r.image.size), 64)
        self.assertEqual(r.original_size, (400, 300))

    def test_oversized_image_rejected_before_decoding(self):
        # 幅・高さだけ巨大なPNGヘッダーを作る（中身は展開されないうちに拒否されるべき）
        p = self.dir / "huge.png"
        Image.new("RGB", (1, 1)).save(p)
        data = bytearray(p.read_bytes())
        data[16:24] = struct.pack(">II", 50000, 50000)  # IHDR の幅・高さを書き換え
        data[29:33] = struct.pack(">I", zlib.crc32(bytes(data[12:29])))  # 正しいチェックサムに直す
        p.write_bytes(bytes(data))
        with self.assertRaises(ImageLoadError) as cm:
            image_io.load_image(p)
        self.assertIn("大きすぎ", cm.exception.cause)
        p.unlink()

    def test_folder_scan_filters_and_continues(self):
        files = list(image_io.iter_candidate_files(self.dir))
        names = {f.name for f in files}
        self.assertNotIn("anim.gif", names)
        self.assertNotIn("memo.txt", names)
        self.assertIn("sample.jfif", names)
        self.assertIn("大文字拡張子.JPG", names)
        ok, ng = 0, 0
        for f in files:  # 1枚壊れていても残りの処理を続けられること
            try:
                image_io.load_image(f, max_side=32)
                ok += 1
            except ImageLoadError:
                ng += 1
        self.assertEqual(ng, 2)  # broken.jpg と not_image.jpg
        self.assertEqual(ok, len(files) - 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
