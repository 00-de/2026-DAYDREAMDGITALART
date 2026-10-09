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

    # ---- 表示 ----
    def set_image(self, img: Image.Image | None, keep_zoom: bool = False) -> None:
        if img is None:
            self._item.setPixmap(QPixmap())
            self.scene().setSceneRect(0, 0, 1, 1)
            self.viewport().update()
            return
        self._item.setPixmap(pil_to_qpixmap(img))
        self.scene().setSceneRect(self._item.boundingRect())
        if self._fit or not keep_zoom:
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
        self.zoomChanged.emit(self.transform().m11())

    def zoom(self, factor: float) -> None:
        cur = self.transform().m11()
        if not (0.05 <= cur * factor <= 20):
            return
        self._fit = False
        self.scale(factor, factor)
        self.zoomChanged.emit(self.transform().m11())

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
