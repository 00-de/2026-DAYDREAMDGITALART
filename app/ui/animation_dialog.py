"""アニメーションの書き出し設定：形式（MP4／GIF／PNG連番）・保存先・名前。"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QRadioButton, QVBoxLayout,
)


class AnimationExportDialog(QDialog):
    def __init__(self, settings: QSettings, mp4_codec: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("アニメーションを書き出す")
        self.setMinimumWidth(560)
        self.settings = settings
        v = QVBoxLayout(self)
        f = QFormLayout()

        self.rb_mp4 = QRadioButton(f"MP4 動画（ショート動画・SNS向け）　圧縮方式：{mp4_codec}" if mp4_codec
                                   else "MP4 動画（このパソコンでは使えません）")
        self.rb_mp4.setEnabled(bool(mp4_codec))
        self.rb_gif = QRadioButton("GIF アニメ（LINE・ブログ向け／軽く小さめ）")
        self.rb_png = QRadioButton("PNG 連番（VEGAS・DaVinci などの動画編集ソフト向け／最高画質）")
        g = QButtonGroup(self)
        for rb in (self.rb_mp4, self.rb_gif, self.rb_png):
            g.addButton(rb)
        last = settings.value("anim/fmt", "mp4")
        {"mp4": self.rb_mp4 if mp4_codec else self.rb_gif, "gif": self.rb_gif, "png": self.rb_png}.get(
            last, self.rb_gif).setChecked(True)
        fv = QVBoxLayout()
        for rb in (self.rb_mp4, self.rb_gif, self.rb_png):
            fv.addWidget(rb)
        f.addRow("形式", fv)

        pictures = Path.home() / "Videos"
        if not pictures.is_dir():
            pictures = Path.home()
        self.ed_folder = QLineEdit(settings.value("anim/folder", str(pictures)))
        b = QPushButton("参照…")
        b.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.ed_folder, 1)
        row.addWidget(b)
        f.addRow("保存先フォルダー", row)
        self.ed_name = QLineEdit(f"DayDreamPlus_文字アニメ_{time.strftime('%Y%m%d_%H%M')}")
        f.addRow("名前", self.ed_name)
        v.addLayout(f)
        self.lbl = QLabel("※ PNG 連番は、この名前のフォルダーを作って 1コマずつ保存します。", objectName="hint")
        v.addWidget(self.lbl)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Save).setText("書き出す")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "保存先フォルダーを選択", self.ed_folder.text())
        if d:
            self.ed_folder.setText(d)

    def _accept(self) -> None:
        folder = Path(self.ed_folder.text().strip())
        name = self.ed_name.text().strip()
        if not name or any(ch in set('\\/:*?"<>|') for ch in name):
            self.lbl.setText('⚠ 名前に \\ / : * ? " < > | は使えません。')
            return
        if not folder.is_dir():
            self.lbl.setText("⚠ 保存先フォルダーが見つかりません。「参照…」で選んでください。")
            return
        fmt = "mp4" if self.rb_mp4.isChecked() else "gif" if self.rb_gif.isChecked() else "png"
        target = folder / (name + {"mp4": ".mp4", "gif": ".gif", "png": ""}[fmt])
        if target.exists():
            r = QMessageBox.question(self, "上書きの確認", f"「{target.name}」はすでにあります。上書きしますか？")
            if r != QMessageBox.StandardButton.Yes:
                return
        self.settings.setValue("anim/fmt", fmt)
        self.settings.setValue("anim/folder", str(folder))
        self.result_format, self.result_path = fmt, str(target)
        self.accept()
