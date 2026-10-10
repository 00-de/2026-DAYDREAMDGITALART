"""画面：① 写真コレクション欄（追加・進捗・キャンセル・サムネイル一覧・詳細）。"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QMenu, QDialogButtonBox, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QListView, QListWidget, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
)

from PySide6.QtWidgets import QWidget as QWidget_

from .. import collection as col
from .. import config
from . import workers

IMAGE_FILTER = "写真 (*.jpg *.jpeg *.jfif *.png *.JPG *.JPEG *.JFIF *.PNG)"


class ThumbModel(QAbstractListModel):
    """サムネイル一覧のデータ。画像は表示が必要になったときだけ作る（2,000枚でも軽い）。"""

    def __init__(self, collection: col.PhotoCollection, show_names: bool = False, parent=None):
        super().__init__(parent)
        self.collection = collection
        self.show_names = show_names
        self._cache: OrderedDict[int, QPixmap] = OrderedDict()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.collection.records)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.collection.records):
            return None
        rec = self.collection.records[index.row()]
        if role == Qt.ItemDataRole.DecorationRole:
            pm = self._cache.get(index.row())
            if pm is None:
                pm = QPixmap()
                pm.loadFromData(rec.thumb_jpeg, "JPG")
                self._cache[index.row()] = pm
                if len(self._cache) > 400:  # 古いものから捨ててメモリを節約
                    self._cache.popitem(last=False)
            else:
                self._cache.move_to_end(index.row())
            return pm
        if role == Qt.ItemDataRole.DisplayRole and self.show_names:
            return Path(rec.path).name
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = f"{Path(rec.path).name}\n{rec.width:,}×{rec.height:,}\n{rec.path}"
            return tip + (f"\n⚠ {rec.warning}" if rec.warning else "")
        return None

    def refresh(self) -> None:
        self.beginResetModel()
        self._cache.clear()
        self.endResetModel()


def _thumb_view(model: ThumbModel, strip: bool) -> QListView:
    v = QListView()
    v.setModel(model)
    v.setViewMode(QListView.ViewMode.IconMode)
    v.setUniformItemSizes(True)
    v.setMovement(QListView.Movement.Static)
    v.setResizeMode(QListView.ResizeMode.Adjust)
    v.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    if strip:
        v.setIconSize(QSize(64, 64))
        v.setGridSize(QSize(72, 72))
        v.setFlow(QListView.Flow.LeftToRight)
        v.setWrapping(False)
        v.setFixedHeight(92)
        v.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    else:
        v.setIconSize(QSize(96, 96))
        v.setGridSize(QSize(120, 128))
        v.setWordWrap(True)
        v.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    return v


class CollectionDetailDialog(QDialog):
    def __init__(self, collection: col.PhotoCollection, parent=None, on_remove=None):
        super().__init__(parent)
        self.setWindowTitle("写真コレクションの詳細")
        self.resize(900, 640)
        self.collection = collection
        self.on_remove = on_remove
        v = QVBoxLayout(self)
        tabs = QTabWidget()
        self.tabs = tabs

        self.model = ThumbModel(collection, show_names=True, parent=self)
        page = QWidget_()
        pv = QVBoxLayout(page)
        pv.setContentsMargins(0, 0, 0, 0)
        self.grid = _thumb_view(self.model, strip=False)
        bar = QHBoxLayout()
        hint = QLabel("写真をクリックして選び（Ctrl＋クリックで複数・Ctrl＋Aで全部）、「選んだ写真を外す」で除外できます。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        self.btn_remove = QPushButton("選んだ写真を外す")
        self.btn_remove.setEnabled(False)
        self.btn_remove.clicked.connect(self._remove_selected)
        bar.addWidget(hint, 1)
        bar.addWidget(self.btn_remove)
        pv.addLayout(bar)
        pv.addWidget(self.grid)
        self.grid.selectionModel().selectionChanged.connect(self._sel_changed)
        self.grid.customContextMenuRequested.connect(self._menu)
        act = QAction("選んだ写真を外す", self.grid)
        act.setShortcut(QKeySequence.StandardKey.Delete)
        act.triggered.connect(self._remove_selected)
        self.grid.addAction(act)
        tabs.addTab(page, f"読み込めた写真（{len(collection.records):,}枚）")

        table = QTableWidget(len(collection.failed), 2)
        table.setHorizontalHeaderLabels(["ファイル", "理由"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(0, 320)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, (p, reason) in enumerate(collection.failed):
            it = QTableWidgetItem(Path(p).name)
            it.setToolTip(p)
            table.setItem(i, 0, it)
            table.setItem(i, 1, QTableWidgetItem(reason))
        tabs.addTab(table, f"読み込めなかった写真（{len(collection.failed):,}枚）")

        dup = QListWidget()
        for p in collection.duplicates:
            dup.addItem(p)
        tabs.addTab(dup, f"重複のため除外（{len(collection.duplicates):,}枚）")

        v.addWidget(tabs)
        note = QLabel("※ 「外す」はコレクションから除くだけで、元の写真ファイルは削除されません。読み込めなかった写真はモザイクに使われません。")
        note.setObjectName("hint")
        v.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.button(QDialogButtonBox.StandardButton.Close).setText("閉じる")
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _sel_changed(self, *_):
        n = len(self.grid.selectionModel().selectedIndexes())
        self.btn_remove.setEnabled(n > 0)
        self.btn_remove.setText(f"選んだ写真を外す（{n}枚）" if n else "選んだ写真を外す")

    def _menu(self, pos):
        n = len(self.grid.selectionModel().selectedIndexes())
        if not n:
            return
        m = QMenu(self)
        m.addAction(f"選んだ写真を外す（{n}枚）", self._remove_selected)
        m.exec(self.grid.viewport().mapToGlobal(pos))

    def _remove_selected(self):
        rows = [i.row() for i in self.grid.selectionModel().selectedIndexes()]
        if rows and self.on_remove:
            self.on_remove(rows)
            self.model.refresh()
            self.tabs.setTabText(0, f"読み込めた写真（{len(self.collection.records):,}枚）")
            self._sel_changed()


class CollectionPanel(QGroupBox):
    changed = Signal(int)        # 登録枚数が変わった
    message = Signal(str, int)   # ステータスバーに出す文（表示ミリ秒）

    def __init__(self, settings: QSettings, parent=None):
        super().__init__("① 写真コレクション（タイルに使う写真・最大2,000枚）", parent)
        self.setObjectName("collectionBox")
        self.setProperty("dropping", False)
        self.settings = settings
        self.collection = col.PhotoCollection()
        self._worker: workers.Worker | None = None
        self._pending_sources: list[str] = []

        v = QVBoxLayout(self)
        v.setSpacing(6)
        row = QHBoxLayout()
        self.btn_folder = QPushButton("📁 フォルダーを追加")
        self.btn_files = QPushButton("🖼 写真ファイルを追加")
        self.btn_folder.setToolTip("フォルダーの中の写真をまとめて追加します")
        self.btn_files.setToolTip("写真を選んで追加します（Ctrl＋A で全部選択、Ctrl＋クリックで複数選択）")
        self.chk_recursive = QCheckBox("サブフォルダーも含める")
        self.chk_recursive.setChecked(self.settings.value("collection/recursive", False, type=bool))
        self.chk_recursive.toggled.connect(lambda on: self.settings.setValue("collection/recursive", on))
        self.lbl_count = QLabel("写真はまだありません　—　ここに写真やフォルダーをドラッグしても追加できます")
        self.lbl_count.setObjectName("hint")
        self.lbl_count.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.btn_detail = QPushButton("詳細")
        self.btn_remove = QPushButton("選んだ写真を外す")
        self.btn_remove.setToolTip("サムネイルをクリックして選び（Ctrl＋クリックで複数）、コレクションから外します。\n元の写真ファイルは削除されません。")
        self.btn_remove.setVisible(False)
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear = QPushButton("すべて外す")
        self.btn_folder.clicked.connect(self.choose_folder)
        self.btn_files.clicked.connect(self.choose_files)
        self.btn_detail.clicked.connect(self.show_details)
        self.btn_clear.clicked.connect(self.clear)
        for w in (self.btn_folder, self.btn_files, self.chk_recursive):
            row.addWidget(w)
        row.addWidget(self.lbl_count, 1)
        row.addWidget(self.btn_remove)
        row.addWidget(self.btn_detail)
        row.addWidget(self.btn_clear)
        v.addLayout(row)

        prow = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.lbl_progress = QLabel("")
        self.lbl_progress.setObjectName("hint")
        self.btn_cancel = QPushButton("読み込みを中止")
        self.btn_cancel.clicked.connect(self.cancel)
        prow.addWidget(self.progress, 1)
        prow.addWidget(self.lbl_progress)
        prow.addWidget(self.btn_cancel)
        self._progress_row = [self.progress, self.lbl_progress, self.btn_cancel]
        v.addLayout(prow)

        self.model = ThumbModel(self.collection, parent=self)
        self.view = _thumb_view(self.model, strip=True)
        self.view.selectionModel().selectionChanged.connect(self._on_selection)
        self.view.customContextMenuRequested.connect(self._context_menu)
        act = QAction("選んだ写真を外す", self.view)
        act.setShortcut(QKeySequence.StandardKey.Delete)
        act.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        act.triggered.connect(self.remove_selected)
        self.view.addAction(act)
        v.addWidget(self.view)
        self.collection.excluded = set(self.settings.value("collection/excluded", [], type=list) or [])
        self.collection.excluded_sha1 = set(self.settings.value("collection/excluded_sha1", [], type=list) or [])
        self._set_busy(False)
        self._update_label()

    # ---- 追加 ----
    def choose_folder(self) -> None:
        start = self.settings.value("paths/folder", str(Path.home() / "Pictures"))
        d = QFileDialog.getExistingDirectory(self, "タイル用写真のフォルダーを選択（写真が入ったフォルダーを開いて「フォルダーの選択」）", start)
        if d:
            self.settings.setValue("paths/folder", d)
            self.add_sources([d])

    def choose_files(self) -> None:
        start = self.settings.value("paths/files", str(Path.home() / "Pictures"))
        files, _ = QFileDialog.getOpenFileNames(self, "タイル用の写真を選択（Ctrl＋A で全部選択できます）", start,
                                                f"{IMAGE_FILTER};;すべてのファイル (*)")
        if files:
            self.settings.setValue("paths/files", str(Path(files[0]).parent))
            self.add_sources(files)

    def add_sources(self, sources: list[str], restoring: bool = False) -> None:
        if self._worker is not None:
            self._pending_sources.extend(sources)  # 読み込み中なら終わったあとに続けて追加
            self.message.emit("読み込みが終わったら、続けて追加します。", 4000)
            return
        if self.collection.capacity == 0:
            QMessageBox.information(self, "上限に達しています",
                                    f"写真は最大{config.MAX_TILE_PHOTOS:,}枚までです。\n「すべて外す」か、写真を減らしてから追加してください。")
            return
        explicit_files = [x for x in sources if Path(x).is_file()]
        if not restoring:  # 利用者が明示的に選び直したファイルは、外した写真でも戻す
            self.collection.unexclude(explicit_files)
        self._set_busy(True)
        self.lbl_progress.setText("前回の写真を確認しています…" if restoring else "写真を探しています…")
        self.progress.setRange(0, 0)
        w = workers.Worker(col.analyze_sources, list(sources), recursive=self.chk_recursive.isChecked(),
                           known_paths=self.collection.skip_paths(),
                           known_sha1=self.collection.known_sha1(include_excluded=restoring or not explicit_files),
                           capacity=self.collection.capacity, with_progress=True)
        w.signals.progress.connect(self._on_progress)
        w.signals.finished.connect(lambda r: self._on_finished(r, sources, restoring))
        w.signals.failed.connect(self._on_failed)
        self._worker = workers.start(w)

    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.lbl_progress.setText(f"写真を確認中… {done:,} / {total:,}枚")

    def _on_finished(self, r: col.BatchResult, sources: list[str], restoring: bool) -> None:
        self._worker = None
        self.collection.merge(r, sources)
        self.model.refresh()
        self._set_busy(False)
        self._update_label()
        self._save_sources()
        parts = [f"{len(r.records):,}枚を追加しました"]
        if r.failed:
            parts.append(f"読み込めなかった写真 {len(r.failed):,}枚（「詳細」で確認できます）")
        if r.duplicates:
            parts.append(f"重複 {len(r.duplicates):,}枚は除外")
        if r.ignored:
            parts.append(f"対象外のファイル {r.ignored:,}個")
        if r.already:
            parts.append(f"登録済み {r.already:,}枚")
        if r.cancelled:
            parts.insert(0, "読み込みを中止しました")
        parts.append(f"{r.seconds:.1f}秒")
        self.message.emit("　".join(parts), 12000)
        if r.over_limit:
            QMessageBox.information(self, "上限に達しました",
                                    f"写真は最大{config.MAX_TILE_PHOTOS:,}枚までのため、{r.over_limit:,}枚は追加しませんでした。")
        elif not restoring and not r.records and not r.cancelled and not r.duplicates and not r.already:
            QMessageBox.information(self, "写真が見つかりません",
                                    "JPG・JPEG・JFIF・PNG の写真が見つかりませんでした。\n"
                                    "別のフォルダーを選ぶか、「サブフォルダーも含める」をオンにしてください。")
        self.changed.emit(len(self.collection))
        if self._pending_sources:
            nxt, self._pending_sources = self._pending_sources, []
            self.add_sources(nxt)

    def _on_failed(self, e) -> None:
        self._worker = None
        self._set_busy(False)
        QMessageBox.warning(self, "読み込みエラー", f"写真の読み込み中にエラーが起きました。\n\n{e}")

    def cancel(self) -> None:
        if self._worker:
            self._worker.cancel()
            self._pending_sources.clear()
            self.lbl_progress.setText("中止しています…")

    def clear(self) -> None:
        if not len(self.collection):
            return
        r = QMessageBox.question(self, "すべて外す",
                                 f"登録した{len(self.collection):,}枚の写真をコレクションから外します。\n"
                                 "（元の写真ファイルは削除されません）")
        if r != QMessageBox.StandardButton.Yes:
            return
        self.collection.clear()
        self.model.refresh()
        self._update_label()
        self._save_sources()
        self.changed.emit(0)

    def show_details(self) -> None:
        CollectionDetailDialog(self.collection, self, on_remove=self._remove_rows).exec()

    # ---- 写真を外す ----
    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.view.selectionModel().selectedIndexes()})

    def _on_selection(self, *_) -> None:
        n = len(self._selected_rows())
        self.btn_remove.setVisible(n > 0)
        self.btn_remove.setText(f"選んだ写真を外す（{n}枚）")

    def _context_menu(self, pos) -> None:
        rows = self._selected_rows()
        if not rows:
            idx = self.view.indexAt(pos)
            if not idx.isValid():
                return
            self.view.selectionModel().select(idx, self.view.selectionModel().SelectionFlag.ClearAndSelect)
            rows = [idx.row()]
        m = QMenu(self)
        m.addAction(f"選んだ写真を外す（{len(rows)}枚）", self.remove_selected)
        m.addAction("すべて選択", self.view.selectAll)
        m.exec(self.view.viewport().mapToGlobal(pos))

    def remove_selected(self) -> None:
        rows = self._selected_rows()
        if rows:
            self._remove_rows(rows)

    def _remove_rows(self, rows: list[int]) -> None:
        if self.is_busy():
            self.message.emit("読み込み中は外せません。読み込みが終わってからお試しください。", 5000)
            return
        removed = self.collection.remove_indices(rows)
        self.model.refresh()
        self._update_label()
        self._on_selection()
        self._save_sources()
        self.message.emit(f"{len(removed)}枚をコレクションから外しました（元の写真ファイルは残っています）。", 8000)
        self.changed.emit(len(self.collection))

    # ---- 前回の写真の自動復元 ----
    def restore_previous(self) -> None:
        sources = self.settings.value("collection/sources", [], type=list) or []
        sources = [s for s in sources if Path(s).exists()]
        if sources:
            self.add_sources(sources, restoring=True)

    def _save_sources(self) -> None:
        self.settings.setValue("collection/sources", self.collection.sources)
        self.settings.setValue("collection/excluded", sorted(self.collection.excluded))
        self.settings.setValue("collection/excluded_sha1", sorted(self.collection.excluded_sha1))

    # ---- 表示 ----
    def set_drop_highlight(self, on: bool, count: int = 0) -> None:
        """写真をドラッグ中、この欄を点線で囲んで「ここに追加されます」と案内する。"""
        if bool(self.property("dropping")) == on and not on:
            return
        self.setProperty("dropping", on)
        self.style().unpolish(self)
        self.style().polish(self)
        if on:
            self.lbl_count.setText(f"⬇ ここで離すと、タイル用の写真に追加されます（{count}個）")
            self.lbl_count.setObjectName("summary")
            self.lbl_count.style().unpolish(self.lbl_count)
            self.lbl_count.style().polish(self.lbl_count)
        else:
            self._update_label()

    def is_busy(self) -> bool:
        return self._worker is not None

    def _set_busy(self, busy: bool) -> None:
        for w in self._progress_row:
            w.setVisible(busy)
        self.btn_clear.setEnabled(not busy)
        self.chk_recursive.setEnabled(not busy)

    def _update_label(self) -> None:
        n = len(self.collection)
        if n == 0:
            self.lbl_count.setText("写真はまだありません　—　ここに写真やフォルダーをドラッグしても追加できます")
            self.lbl_count.setObjectName("hint")
        else:
            extra = []
            if self.collection.failed:
                extra.append(f"失敗 {len(self.collection.failed):,}")
            if self.collection.duplicates:
                extra.append(f"重複 {len(self.collection.duplicates):,}")
            self.lbl_count.setText(f"登録 {n:,} / {config.MAX_TILE_PHOTOS:,}枚" + (f"（{'・'.join(extra)}）" if extra else ""))
            self.lbl_count.setObjectName("summary")
        self.lbl_count.style().unpolish(self.lbl_count)
        self.lbl_count.style().polish(self.lbl_count)
        self.btn_detail.setEnabled(bool(n or self.collection.failed or self.collection.duplicates))
        self.view.setVisible(n > 0)
