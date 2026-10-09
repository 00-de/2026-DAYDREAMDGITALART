"""文字マスク生成のテスト（追加仕様書 7章の受け入れテストのうちフェーズ1で確認できるもの）。"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import text_mask as tm  # noqa: E402
from app.errors import TextInputError  # noqa: E402

TEXT_5 = "ドリプラ!"
TEXT_10 = "DayDream ✨"   # 絵文字を含む
TEXT_10_JA = "垂井町からありがとう"
TEXT_40 = "DayDream Plus 2027 垂井町文化会館ライブ みんなで最高の思い出"


class CountTest(unittest.TestCase):
    def test_counts_user_perceived_chars(self):
        self.assertEqual(tm.count_chars("あいう"), 3)
        self.assertEqual(tm.count_chars("👨‍👩‍👧"), 1)       # 家族の絵文字（7コードポイント）
        self.assertEqual(tm.count_chars("が"), 1)     # か＋濁点の合成文字
        self.assertEqual(tm.count_chars("A B"), 3)          # スペースは数える
        self.assertEqual(tm.count_chars("あ\nい"), 2)       # 改行は数えない

    def test_sample_lengths(self):
        self.assertEqual(tm.count_chars(TEXT_5), 5)
        self.assertEqual(tm.count_chars(TEXT_10), 10)
        self.assertEqual(tm.count_chars(TEXT_10_JA), 10)
        self.assertEqual(tm.count_chars(TEXT_40), 40)

    def test_limits(self):
        with self.assertRaises(TextInputError):
            tm.validate_text("")
        with self.assertRaises(TextInputError):
            tm.validate_text("   \n  ")
        tm.validate_text("あ")                 # 1文字はOK
        tm.validate_text("あ" * 40)            # 40文字はOK
        with self.assertRaises(TextInputError):
            tm.validate_text("あ" * 41)         # 41文字はNG

    def test_truncate_blocks_41st_char(self):
        self.assertEqual(tm.count_chars(tm.truncate_to_limit("あ" * 50)), 40)
        self.assertEqual(tm.truncate_to_limit("👨‍👩‍👧" * 41), "👨‍👩‍👧" * 40)  # 絵文字を途中で割らない


class MaskTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font = tm.default_font()

    def _check_fits(self, r, settings):
        cw, ch = settings.canvas_size
        margin = int(min(cw, ch) * settings.margin)
        l, t, rr, b = r.text_box
        self.assertGreaterEqual(l, margin)
        self.assertGreaterEqual(t, margin)
        self.assertLessEqual(rr, cw - margin)
        self.assertLessEqual(b, ch - margin)
        arr = r.as_array()
        self.assertEqual(arr.shape, (ch, cw))
        self.assertTrue(set(np.unique(arr)) <= {0, 255})       # 完全な白黒
        # 余白部分に文字がはみ出していない
        self.assertEqual(arr[:margin].max(), 0)
        self.assertEqual(arr[-margin:].max(), 0)
        self.assertEqual(arr[:, :margin].max(), 0)
        self.assertEqual(arr[:, -margin:].max(), 0)

    def test_5_10_40_chars_generate_and_fit(self):
        for text in (TEXT_5, TEXT_10_JA, TEXT_40):
            for multiline in (True, False):
                s = tm.TextMaskSettings(multiline=multiline)
                r = tm.render_text_mask(text, s)
                self._check_fits(r, s)
                self.assertGreater(r.as_array().max(), 0, f"文字が描かれていない：{text}")
                if multiline:
                    self.assertGreater(r.as_array().mean(), 255 * 0.03, f"文字が薄すぎる：{text}")
                elif r.font_size < min(s.canvas_size) * 0.04:
                    # 長文の1行表示は小さくなるので、利用者へ警告が出ること
                    self.assertTrue(any("小さく" in w for w in r.warnings))

    def test_single_line_mode_is_one_line(self):
        r = tm.render_text_mask("ありがとう\nDayDream Plus", tm.TextMaskSettings(multiline=False))
        self.assertEqual(len(r.lines), 1)

    def test_explicit_newline_respected(self):
        r = tm.render_text_mask("ありがとう\nDayDream Plus", tm.TextMaskSettings(multiline=True))
        self.assertEqual(r.lines[0], "ありがとう")

    def test_english_words_not_split(self):
        r = tm.render_text_mask(TEXT_40)
        joined = "|".join(r.lines)
        self.assertNotIn("DayD|", joined)
        self.assertNotIn("Pl|us", joined)
        self.assertTrue(any("DayDream" in ln for ln in r.lines))

    def test_no_punctuation_at_line_start(self):
        r = tm.render_text_mask("みんなありがとう。またライブで会おうね、待ってるよ！")
        for ln in r.lines[1:]:
            self.assertNotIn(ln[0], "、。！")

    def test_weight_makes_text_thicker(self):
        thin = tm.render_text_mask(TEXT_10_JA, tm.TextMaskSettings(weight=0, font_size=200))
        bold = tm.render_text_mask(TEXT_10_JA, tm.TextMaskSettings(weight=6, font_size=200))
        self.assertGreater(bold.as_array().mean(), thin.as_array().mean() * 1.2)

    def test_letter_spacing_widens(self):
        a = tm.render_text_mask("あいう", tm.TextMaskSettings(letter_spacing=0.0, font_size=150))
        b = tm.render_text_mask("あいう", tm.TextMaskSettings(letter_spacing=0.5, font_size=150))
        self.assertGreater(b.text_box[2] - b.text_box[0], a.text_box[2] - a.text_box[0])

    def test_too_large_font_size_is_shrunk_with_warning(self):
        s = tm.TextMaskSettings(font_size=3000)
        r = tm.render_text_mask(TEXT_10_JA, s)
        self._check_fits(r, s)
        self.assertTrue(any("縮小" in w for w in r.warnings))

    def test_missing_glyph_warning(self):
        r = tm.render_text_mask("Hi😀ドリプラ")
        self.assertTrue(any("表示できない文字" in w for w in r.warnings))

    def test_recommended_tiles_keep_kanji_readable(self):
        r = tm.render_text_mask(TEXT_40)
        tile = tm.recommend_tile_px(r)
        cols, rows = tm.grid_size_for(r.mask.size, tile)
        self.assertGreaterEqual(r.font_size / tile, tm.MIN_TILES_PER_CHAR)
        cov = tm.mask_coverage(r.mask, cols, rows)
        self.assertEqual(cov.shape, (rows, cols))
        self.assertTrue(0 < cov.max() <= 1)

    def test_japanese_font_found(self):
        self.assertTrue(len(tm.find_japanese_fonts()) > 0)
        self.assertFalse(tm._is_missing_glyph(
            __import__("PIL.ImageFont", fromlist=["x"]).truetype(self.font.path, 40, index=self.font.index),
            "館"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
