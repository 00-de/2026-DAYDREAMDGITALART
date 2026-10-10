"""アニメーションの書き出し：MP4動画・GIFアニメ・PNG連番。

■ MP4：Qt（LGPLライセンス）の動画機能で作る。
  Windows では OS に入っている H.264 エンコーダー（Media Foundation）を使うので、
  追加のライセンス料や GPL の部品は不要。H.264 が使えない環境では MPEG-4 で作る。
■ GIF：Pillow で作る（SNSやLINE向けの軽い動き）。大きさは自動で半分程度に縮める。
■ PNG連番：1コマずつの画像。VEGAS や DaVinci Resolve に「画像シーケンス」として読み込める。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable

from PIL import Image
from PySide6.QtCore import QEventLoop, QSize, QTimer, QUrl
from PySide6.QtGui import QImage

from .animation import AnimationRenderer
from .errors import MosaicError

Progress = Callable[[int, int], None]
Cancelled = Callable[[], bool]


class ExportCancelled(MosaicError):
    pass


def _check_space(folder: Path, need: int) -> None:
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    if need * 1.2 > free:
        raise MosaicError(f"保存先の空き容量が足りません（必要 約{need / 1e6:,.0f}MB／空き {free / 1e6:,.0f}MB）。",
                          "不要なファイルを削除するか、別のドライブに保存してください。")


def qimage_to_pil(img: QImage) -> Image.Image:
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    w, h = img.width(), img.height()
    ptr = img.constBits()
    bpl = img.bytesPerLine()
    data = bytes(ptr)[: bpl * h]
    return Image.frombuffer("RGB", (w, h), data, "raw", "RGB", bpl, 1).copy()


# --------------------------------------------------------------------------
# PNG連番
# --------------------------------------------------------------------------
def export_png_sequence(r: AnimationRenderer, folder: str, progress: Progress | None = None,
                        is_cancelled: Cancelled | None = None) -> tuple[str, int]:
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    _check_space(out, int(r.W * r.H * 3 * 0.5 * r.frame_count))
    n = r.frame_count
    for k in range(n):
        if is_cancelled and is_cancelled():
            raise ExportCancelled("書き出しを中止しました。")
        if not r.frame(k).save(str(out / f"frame_{k + 1:05d}.png"), "PNG"):
            raise MosaicError("画像を書き込めませんでした。", "保存先のフォルダーを確認してください。")
        if progress:
            progress(k + 1, n)
    return str(out), n


# --------------------------------------------------------------------------
# GIF
# --------------------------------------------------------------------------
def export_gif(r: AnimationRenderer, path: str, max_side: int = 720, fps: int = 15,
               progress: Progress | None = None, is_cancelled: Cancelled | None = None) -> str:
    target = Path(path).with_suffix(".gif")
    scale = min(1.0, max_side / max(r.W, r.H))
    step = max(1, round(r.st.fps / fps))
    idx = list(range(0, r.frame_count, step))
    if idx[-1] != r.frame_count - 1:
        idx.append(r.frame_count - 1)
    frames = []
    for n, k in enumerate(idx):
        if is_cancelled and is_cancelled():
            raise ExportCancelled("書き出しを中止しました。")
        frames.append(qimage_to_pil(r.frame(k, scale)).quantize(colors=255, method=Image.Quantize.MEDIANCUT))
        if progress:
            progress(n + 1, len(idx) + 1)
    durations = [int(1000 * step / r.st.fps)] * len(frames)
    durations[-1] = int(r.st.hold * 1000)  # 最後のコマは長めに見せる
    tmp = target.with_name("." + target.name + ".saving")
    frames[0].save(tmp, "GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   optimize=False, disposal=1)
    os.replace(tmp, target)
    if progress:
        progress(len(idx) + 1, len(idx) + 1)
    return str(target)


# --------------------------------------------------------------------------
# MP4（画面と同じメインスレッドで呼ぶこと）
# --------------------------------------------------------------------------
def mp4_codec_name() -> str:
    """この PC で使える動画の圧縮方式（H.264 か MPEG-4）。"""
    from PySide6.QtMultimedia import QMediaFormat
    fmt = QMediaFormat(QMediaFormat.FileFormat.MPEG4)
    enc = fmt.supportedVideoCodecs(QMediaFormat.ConversionMode.Encode)
    if QMediaFormat.VideoCodec.H264 in enc:
        return "H.264"
    if QMediaFormat.VideoCodec.MPEG4 in enc:
        return "MPEG-4"
    return ""


def export_mp4(r: AnimationRenderer, path: str, progress: Progress | None = None,
               is_cancelled: Cancelled | None = None, timeout_sec: float = 600) -> tuple[str, str]:
    """MP4 動画を書き出す。戻り値：(保存先, 使った圧縮方式)。"""
    from PySide6.QtMultimedia import (QMediaCaptureSession, QMediaFormat, QMediaRecorder, QVideoFrame,
                                      QVideoFrameFormat, QVideoFrameInput)
    target = Path(path).with_suffix(".mp4")
    _check_space(target.parent, int(r.W * r.H * 0.15 * r.frame_count))
    fmt = QMediaFormat(QMediaFormat.FileFormat.MPEG4)
    enc = fmt.supportedVideoCodecs(QMediaFormat.ConversionMode.Encode)
    if QMediaFormat.VideoCodec.H264 in enc:
        codec, codec_name = QMediaFormat.VideoCodec.H264, "H.264"
    elif QMediaFormat.VideoCodec.MPEG4 in enc:
        codec, codec_name = QMediaFormat.VideoCodec.MPEG4, "MPEG-4"
    else:
        raise MosaicError("このパソコンでは MP4 動画を作れませんでした。", "GIF または PNG連番で書き出してください。")
    fmt.setVideoCodec(codec)
    fmt.setAudioCodec(QMediaFormat.AudioCodec.Unspecified)

    tmp = target.with_name(".saving_" + target.name)
    if tmp.exists():
        tmp.unlink()
    session = QMediaCaptureSession()
    vfi = QVideoFrameInput(QVideoFrameFormat(QSize(r.W, r.H), QVideoFrameFormat.PixelFormat.Format_BGRA8888))
    session.setVideoFrameInput(vfi)
    rec = QMediaRecorder()
    session.setRecorder(rec)
    rec.setMediaFormat(fmt)
    rec.setVideoResolution(QSize(r.W, r.H))
    rec.setVideoFrameRate(r.st.fps)
    rec.setQuality(QMediaRecorder.Quality.VeryHighQuality)
    rec.setEncodingMode(QMediaRecorder.EncodingMode.ConstantQualityEncoding)
    rec.setOutputLocation(QUrl.fromLocalFile(str(tmp)))

    n = r.frame_count
    st = {"k": 0, "err": "", "stopping": False, "cancel": False}
    loop = QEventLoop()

    def finish():
        if not st["stopping"]:
            st["stopping"] = True
            rec.stop()

    def send():
        while st["k"] < n and not st["stopping"]:
            if is_cancelled and is_cancelled():
                st["cancel"] = True
                finish()
                return
            img = r.frame(st["k"]).convertToFormat(QImage.Format.Format_ARGB32)
            f = QVideoFrame(img)
            f.setStartTime(int(st["k"] * 1_000_000 / r.st.fps))
            f.setEndTime(int((st["k"] + 1) * 1_000_000 / r.st.fps))
            if not vfi.sendVideoFrame(f):
                return  # 受け取り側が忙しい → 準備ができたら readyToSendVideoFrame で再開
            st["k"] += 1
            if progress:
                progress(st["k"], n + 1)
        if st["k"] >= n:
            finish()

    def on_state(state):
        if state == QMediaRecorder.RecorderState.StoppedState:
            loop.quit()

    def on_error(err, msg):
        st["err"] = msg or str(err)
        loop.quit()

    vfi.readyToSendVideoFrame.connect(send)
    rec.recorderStateChanged.connect(on_state)
    rec.errorOccurred.connect(on_error)
    rec.record()
    QTimer.singleShot(0, send)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(lambda: (st.update(err="時間がかかりすぎたため中止しました"), loop.quit()))
    guard.start(int(timeout_sec * 1000))
    loop.exec()
    guard.stop()
    if rec.recorderState() != QMediaRecorder.RecorderState.StoppedState:
        rec.stop()

    if st["cancel"]:
        _remove(tmp)
        raise ExportCancelled("書き出しを中止しました。")
    if st["err"] or not tmp.exists() or tmp.stat().st_size == 0:
        _remove(tmp)
        raise MosaicError(f"MP4 動画を書き出せませんでした（{st['err'] or '不明なエラー'}）。",
                          "GIF または PNG連番で書き出してください。")
    os.replace(tmp, target)
    if progress:
        progress(n + 1, n + 1)
    return str(target), codec_name


def _remove(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass
