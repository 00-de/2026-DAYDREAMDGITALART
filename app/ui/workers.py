"""重い処理を画面とは別の「作業係（スレッド）」で動かすための仕組み。

画面と同じ場所で重い処理をすると、ウィンドウが「応答なし」になってしまいます。
Worker に処理を渡すと裏で実行し、終わったら結果を画面に知らせます。
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class WorkerSignals(QObject):
    finished = Signal(object)      # 成功：結果
    failed = Signal(object)        # 失敗：例外オブジェクト
    progress = Signal(int, int)    # 進み具合：(済んだ量, 全体量)


class Worker(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args, with_progress: bool = False, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.with_progress = with_progress
        self.signals = WorkerSignals()
        self.cancelled = False
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self.cancelled = True

    def run(self) -> None:
        try:
            if self.with_progress:
                self.kwargs["progress"] = lambda d, t: self.signals.progress.emit(int(d), int(t))
                self.kwargs["is_cancelled"] = lambda: self.cancelled
            result = self.fn(*self.args, **self.kwargs)
        except Exception as e:  # 失敗も画面側で日本語表示する
            e.__traceback_text__ = traceback.format_exc()  # type: ignore[attr-defined]
            try:
                self.signals.failed.emit(e)
            except RuntimeError:
                pass  # 画面が先に閉じられた場合
            return
        try:
            self.signals.finished.emit(result)
        except RuntimeError:
            pass


def start(worker: Worker) -> Worker:
    QThreadPool.globalInstance().start(worker)
    return worker
