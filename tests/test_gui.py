"""画面のテスト（画面を実際には表示せずに動かす）。"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QMimeData, QPointF, QSettings, Qt, QThreadPool, QUrl  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app import config, text_mask  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv[:1])

# テスト中に確認画面が出ても止まらないよう、自動で閉じて内容を記録する
POPUPS: list[str] = []
for _name in ("information", "warning", "critical"):
    setattr(QMessageBox, _name, staticmethod(lambda *a, **k: POPUPS.append(a[2] if len(a) > 2 else "") or QMessageBox.StandardButton.Ok))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)


def pump(cond, timeout=30.0):
    t = time.time()
    while time.time() - t < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


class GuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["LOCALAPPDATA"] = cls.tmp.name      # 保存庫をテスト用の場所に
        os.environ["XDG_DATA_HOME"] = cls.tmp.name
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, cls.tmp.name)
        cls.photos = Path(cls.tmp.name) / "写真"
        cls.photos.mkdir()
        for i in range(12):
            Image.new("RGB", (120, 90), (i * 20, 100, 255 - i * 20)).save(cls.photos / f"p{i}.jpg", "JPEG")
        from app.ui.main_window import MainWindow
        cls.w = MainWindow(check_updates_on_start=False)
        cls.w.show()
        app.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.w.close()
        QThreadPool.globalInstance().waitForDone(5000)
        cls.tmp.cleanup()

    def _drop(self, paths, target):
        """Windows と同じ順番（DragEnter → Drop）で、指定した部品にイベントを届ける。"""
        if hasattr(target, "viewport"):
            target = target.viewport()
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        center = target.rect().center()
        enter = QDragEnterEvent(center, Qt.DropAction.CopyAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(target, enter)
        self.assertTrue(enter.isAccepted(), f"{type(target).__name__} がドラッグを受け付けなかった")
        drop = QDropEvent(QPointF(center), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(target, drop)
        self.assertTrue(drop.isAccepted())

    def test_1_drop_folder_adds_to_collection(self):
        panel = self.w.collection_panel
        self._drop([self.photos], panel)
        self.assertTrue(pump(lambda: not panel.is_busy() and len(panel.collection) == 12))
        self.assertIn("12", panel.lbl_count.text())

    def test_2_drop_image_on_preview_sets_main_photo(self):
        self.w.mode_tabs.setCurrentIndex(0)
        self._drop([self.photos / "p3.jpg"], self.w.preview)
        self.assertTrue(pump(lambda: self.w._main_photo is not None))
        self.assertTrue(self.w._main_photo.path.name == "p3.jpg")

    def test_3_duplicate_add_is_ignored(self):
        panel = self.w.collection_panel
        panel.add_sources([str(self.photos / "p1.jpg")])
        n_popups = len(POPUPS)
        self.assertTrue(pump(lambda: not panel.is_busy()))
        self.assertEqual(len(panel.collection), 12)
        self.assertEqual(len(POPUPS), n_popups, "登録済みの写真で警告を出してはいけない")

    def test_3b_drop_on_any_widget_goes_to_collection(self):
        panel = self.w.collection_panel
        extra = Path(self.tmp.name) / "追加"
        extra.mkdir(exist_ok=True)
        targets = [("サムネイル一覧", panel.view), ("文字の入力欄", self.w.ed_text),
                   ("数値欄", self.w.sp_tile), ("ボタン", self.w.btn_reset)]
        for i, (label, widget) in enumerate(targets):
            imgs = []
            for k in range(3):  # 何枚かまとめてドロップ
                f = extra / f"{label}_{k}.png"
                Image.new("RGB", (40, 40), (i * 60, k * 80, 200)).save(f)
                imgs.append(f)
            before = len(panel.collection)
            if label == "文字の入力欄":
                self.w.mode_tabs.setCurrentIndex(1)
                text_before = self.w.ed_text.toPlainText()
            self._drop(imgs, widget)
            self.assertTrue(pump(lambda: not panel.is_busy() and len(panel.collection) == before + 3), label)
            if label == "文字の入力欄":
                self.assertEqual(self.w.ed_text.toPlainText(), text_before, "入力欄にファイル名が入ってしまった")
                self.w.mode_tabs.setCurrentIndex(0)

    def test_3c_multiple_images_on_preview_go_to_collection(self):
        panel = self.w.collection_panel
        self.w.mode_tabs.setCurrentIndex(0)
        extra = Path(self.tmp.name) / "プレビューへ"
        extra.mkdir(exist_ok=True)
        imgs = []
        for k in range(2):
            f = extra / f"pv{k}.jpg"
            Image.new("RGB", (40, 40), (k * 120, 30, 90)).save(f, "JPEG")
            imgs.append(f)
        before = len(panel.collection)
        self._drop(imgs, self.w.preview)
        self.assertTrue(pump(lambda: not panel.is_busy() and len(panel.collection) == before + 2))
        self.assertFalse(panel.property("dropping"), "ドロップ後も強調表示が残っている")

    def test_4_text_input_limit(self):
        self.w.mode_tabs.setCurrentIndex(1)
        self.w.ed_text.setPlainText("あ" * 45)
        app.processEvents()
        self.assertEqual(text_mask.count_chars(self.w.ed_text.toPlainText()), config.TEXT_MAX_CHARS)
        self.assertIn("40 / 40", self.w.lbl_counter.text())

    def test_5_generate_photo_mosaic_and_save(self):
        from app import exporter
        self.w.mode_tabs.setCurrentIndex(0)
        self.assertTrue(pump(lambda: self.w._main_photo is not None))
        self.w.sp_tile.setValue(10)
        self.w.sp_cols.setValue(20)
        self.assertTrue(self.w.btn_generate.isEnabled(), self.w.btn_generate.toolTip())
        self.w.generate()
        self.assertTrue(pump(lambda: self.w._gen_worker is None, 60))
        self.assertIsNotNone(self.w._result)
        self.assertTrue(self.w.tb_after.isChecked())
        self.assertTrue(self.w.btn_save.isEnabled())
        out = Path(self.tmp.name) / "保存先"
        out.mkdir()
        r = exporter.save_image(self.w._result.image, exporter.SaveOptions(str(out / "作品"), "JPEG", 90))
        self.assertTrue(Path(r.path).exists())

    def test_6_generate_text_mosaic(self):
        if not self.w._fonts:
            self.assertTrue(pump(lambda: bool(self.w._fonts), 30))
        self.w.mode_tabs.setCurrentIndex(1)
        self.w.ed_text.setPlainText("ドリプラ")
        self.w.sp_text_tile.setValue(10)
        self.assertTrue(pump(lambda: self.w.btn_generate.isEnabled()), self.w.btn_generate.toolTip())
        self.w.generate()
        self.assertTrue(pump(lambda: self.w._gen_worker is None, 60))
        self.assertEqual(self.w._result.settings["mode"], "text")

    def test_7_generate_disabled_without_photos(self):
        self.w.collection_panel.clear()
        self.w.mode_tabs.setCurrentIndex(0)
        self.assertFalse(self.w.btn_generate.isEnabled())
        self.assertIn("写真コレクション", self.w.btn_generate.toolTip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
