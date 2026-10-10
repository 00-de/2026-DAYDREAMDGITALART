"""プレビュー表示欄：拡大・縮小・全体表示に対応した画像ビューア。

大きな完成画像そのものではなく、プレビュー用の縮小画像を表示する設計です（仕様書 9章）。
"""

from __future__ import annotations

from PIL import Image
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QWheelEvent
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView


def pil_to_qpixmap(img: Image.Image) -> QPixmap:
    rgb = img.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    qimg = QImage(data, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())  # copy でPythonのデータから切り離す


class PreviewView(QGraphicsView):
    zoomChanged = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene().addItem(self._item)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setObjectName("previewView")
        self._fit = True
        self._placeholder = "ここにプレビューが表示されます"
        self._full: Image.Image | None = None   # 拡大したときに使う、元の大きさの画像
        self._showing_full = False
        self._base_w = 1                          # プレビュー画像の幅（画面上の基準）

    # ---- 表示 ----
    FULL_MAX_SIDE = 12000  # 拡大用の画像の最大の長辺（メモリを使いすぎないため）

    def set_image(self, img: Image.Image | None, keep_zoom: bool = False,
                  full: Image.Image | None = None) -> None:
        """img：表示用の縮小画像。full を渡すと、拡大したときに自動で元の大きさの画像に切り替える。"""
        self._full = full if (full is not None and img is not None and full.width > img.width) else None
        self._showing_full = False
        self._item.setScale(1.0)
        if img is None:
            self._item.setPixmap(QPixmap())
            self.scene().setSceneRect(0, 0, 1, 1)
            self.viewport().update()
            return
        self._base_w = img.width
        self._item.setPixmap(pil_to_qpixmap(img))
        self.scene().setSceneRect(self._item.boundingRect())
        if self._fit or not keep_zoom:
            self.fit()

    def _maybe_show_full(self) -> None:
        """プレビュー画像を引き伸ばして表示し始めたら、元の大きさの画像に差し替える（ぼやけ防止）。"""
        if self._full is None or self._showing_full or self.transform().m11() <= 1.0:
            return
        full = self._full
        if max(full.size) > self.FULL_MAX_SIDE:
            full = full.copy()
            full.thumbnail((self.FULL_MAX_SIDE, self.FULL_MAX_SIDE), Image.Resampling.LANCZOS)
        self._item.setPixmap(pil_to_qpixmap(full))
        self._item.setScale(self._base_w / full.width)  # 画面上の大きさは変えずに中身だけ高精細に
        self._showing_full = True

    def real_zoom(self) -> float:
        """元の画像に対する実際の拡大率（100%＝元の画像の1ピクセルが画面の1ピクセル）。"""
        m = self.transform().m11()
        if self._full is not None:
            return m * self._base_w / self._full.width
        return m

    def set_qimage(self, img: QImage) -> None:
        """アニメーション再生用：QImage をそのまま表示する（変換を省いて軽くする）。"""
        first = (not self.has_image() or self._item.pixmap().size() != img.size()
                 or self._full is not None or self._item.scale() != 1.0)
        self._full, self._showing_full, self._base_w = None, False, img.width()
        self._item.setScale(1.0)
        self._item.setPixmap(QPixmap.fromImage(img))
        if first:
            self.scene().setSceneRect(self._item.boundingRect())
            if self._fit:
                self.fit()

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        self.viewport().update()

    def has_image(self) -> bool:
        return not self._item.pixmap().isNull()

    # ---- 拡大・縮小 ----
    def fit(self) -> None:
        self._fit = True
        if self.has_image():
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
        self._maybe_show_full()
        self.zoomChanged.emit(self.real_zoom())

    def zoom(self, factor: float) -> None:
        cur = self.transform().m11()
        limit = 20 * (self._full.width / self._base_w if self._full is not None else 1)
        if not (0.05 <= cur * factor <= limit):
            return
        self._fit = False
        self.scale(factor, factor)
        self._maybe_show_full()
        self.zoomChanged.emit(self.real_zoom())

    def wheelEvent(self, e: QWheelEvent) -> None:
        self.zoom(1.15 if e.angleDelta().y() > 0 else 1 / 1.15)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self._fit:
            self.fit()

    def drawForeground(self, painter: QPainter, rect) -> None:
        if not self.has_image():
            painter.save()
            painter.resetTransform()
            painter.setPen(QColor("#C9C2DE"))  # 暗い背景でも読める明るい色
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder)
            painter.restore()
