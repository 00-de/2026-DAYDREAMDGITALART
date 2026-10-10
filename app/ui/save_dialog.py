"""保存の設定画面：保存先・ファイル名・形式・解像度・JPEG品質（仕様書 10章）。"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QSlider, QVBoxLayout,
)

from .. import config, exporter


class SaveDialog(QDialog):
    def __init__(self, settings: QSettings, image_size: tuple[int, int], mode: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("完成画像を保存")
        self.setMinimumWidth(560)
        self.settings = settings
        self.image_size = image_size
        v = QVBoxLayout(self)
        f = QFormLayout()

        pictures = Path.home() / "Pictures"
        default_dir = settings.value("save/folder", str(pictures if pictures.is_dir() else Path.home()))
        self.ed_folder = QLineEdit(default_dir)
        b = QPushButton("参照…")
        b.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.ed_folder, 1)
        row.addWidget(b)
        f.addRow("保存先フォルダー", row)

        stamp = time.strftime("%Y%m%d_%H%M")
        kind = "文字モザイク" if mode == "text" else "写真モザイク"
        self.ed_name = QLineEdit(f"DayDreamPlus_{kind}_{stamp}")
        f.addRow("ファイル名", self.ed_name)

        fmt_row = QHBoxLayout()
        self.rb_png = QRadioButton("PNG（高画質・ファイル大きめ）")
        self.rb_jpg = QRadioButton("JPEG（ファイル小さめ・SNS向き）")
        g = QButtonGroup(self)
        g.addButton(self.rb_png)
        g.addButton(self.rb_jpg)
        (self.rb_jpg if settings.value("save/fmt", "PNG") == "JPEG" else self.rb_png).setChecked(True)
        fmt_row.addWidget(self.rb_png)
        fmt_row.addWidget(self.rb_jpg)
        fmt_row.addStretch(1)
        f.addRow("形式", fmt_row)

        q_row = QHBoxLayout()
        self.sl_quality = QSlider(Qt.Orientation.Horizontal)
        self.sl_quality.setRange(60, 100)
        self.sl_quality.setValue(int(settings.value("save/quality", config.DEFAULT_JPEG_QUALITY)))
        self.lbl_quality = QLabel()
        q_row.addWidget(self.sl_quality, 1)
        q_row.addWidget(self.lbl_quality)
        f.addRow("JPEG の画質", q_row)

        self.cb_scale = QComboBox()
        for pct in (100, 75, 50, 25):
            w, h = round(image_size[0] * pct / 100), round(image_size[1] * pct / 100)
            self.cb_scale.addItem(f"{pct}%（{w:,}×{h:,}px）", pct / 100)
        f.addRow("出力解像度", self.cb_scale)

        self.chk_settings = QCheckBox("設定情報（.json）も一緒に保存する")
        self.chk_settings.setToolTip("同じ名前の .json ファイルに、文字・フォント・タイル配置などの設定を記録します。\nあとで同じ作品を作り直すときに使えます。")
        self.chk_settings.setChecked(settings.value("save/settings_json", mode == "text", type=bool))
        f.addRow("", self.chk_settings)
        v.addLayout(f)

        self.lbl_estimate = QLabel("", objectName="hint")
        v.addWidget(self.lbl_estimate)
        note = QLabel("※ 元の写真は上書き・変更されません。", objectName="hint")
        v.addWidget(note)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Save).setText("保存する")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        for sig in (self.rb_png.toggled, self.sl_quality.valueChanged, self.cb_scale.currentIndexChanged):
            sig.connect(self._refresh)
        self._refresh()

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "保存先フォルダーを選択", self.ed_folder.text())
        if d:
            self.ed_folder.setText(d)

    def _fmt(self) -> str:
        return "PNG" if self.rb_png.isChecked() else "JPEG"

    def _refresh(self, *_) -> None:
        jpeg = self._fmt() == "JPEG"
        self.sl_quality.setEnabled(jpeg)
        self.lbl_quality.setText(f"{self.sl_quality.value()}" if jpeg else "—")
        s = self.cb_scale.currentData()
        w, h = round(self.image_size[0] * s), round(self.image_size[1] * s)
        est = exporter.estimate_file_bytes(w, h, self._fmt(), self.sl_quality.value())
        self.lbl_estimate.setText(f"保存後のファイルの大きさ：約 {est / 1e6:,.1f}MB（目安）")

    def _accept(self) -> None:
        folder = Path(self.ed_folder.text().strip())
        name = self.ed_name.text().strip()
        bad = set('\\/:*?"<>|')
        if not name or any(ch in bad for ch in name):
            self.lbl_estimate.setText('⚠ ファイル名に \\ / : * ? " < > | は使えません。')
            return
        if not folder.is_dir():
            self.lbl_estimate.setText("⚠ 保存先フォルダーが見つかりません。「参照…」で選んでください。")
            return
        target = folder / (name + (".png" if self._fmt() == "PNG" else ".jpg"))
        if target.exists():
            from PySide6.QtWidgets import QMessageBox
            r = QMessageBox.question(self, "上書きの確認", f"「{target.name}」はすでにあります。上書きしますか？")
            if r != QMessageBox.StandardButton.Yes:
                return
        self.settings.setValue("save/folder", str(folder))
        self.settings.setValue("save/fmt", self._fmt())
        self.settings.setValue("save/quality", self.sl_quality.value())
        self.settings.setValue("save/settings_json", self.chk_settings.isChecked())
        self.options = exporter.SaveOptions(str(target), self._fmt(), self.sl_quality.value(),
                                            self.cb_scale.currentData(), self.chk_settings.isChecked())
        self.accept()
