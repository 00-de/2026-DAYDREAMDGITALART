"""メイン画面。

上から順に「①写真を入れる → ②作り方を決める（写真モザイク／文字モザイク） → ③確かめる → 生成・保存」。
重い処理はすべて作業係（workers.py）に任せ、画面が固まらないようにしています。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QFileDialog, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QProgressDialog, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSlider,
    QSpinBox, QSplitter, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from .. import animation, config, exporter, image_io, mosaic_engine, paths, text_mask, updater, video_export
from ..errors import MosaicError
from ..version import VERSION
from . import workers
from .collection_view import CollectionPanel
from .preview import PreviewView
from .animation_dialog import AnimationExportDialog
from .save_dialog import SaveDialog
from .style import STYLESHEET

# 文字モザイクのキャンバス形（縦横比）
CANVAS_PRESETS = {
    "横長 3:2": (3, 2),
    "横長 16:9": (16, 9),
    "正方形 1:1": (1, 1),
    "縦長 9:16（ショート動画向け）": (9, 16),
    "縦長 2:3": (2, 3),
}
MASK_LONG_SIDE = 2400   # 文字マスクを作るときの長辺（計算用。出力画像の大きさとは別）
PREVIEW_MAX = 1400      # プレビュー画像の最大幅・高さ（表示を軽くするため）
MIN_GRID_SHORT_SIDE = 60  # 文字が少なくても「写真のモザイク」に見えるよう、短い辺に最低このタイル数


@dataclass
class Defaults:
    tile_px: int = 40
    photo_cols: int = 100
    max_uses: int = 5
    color_match: int = 70
    edge: int = 20
    text: str = "DayDream Plus"
    weight: int = 1
    letter_spacing: int = 5     # ％
    line_spacing: int = 115     # ％
    text_color: str = "#FFFFFF"
    bg_color: str = "#1E1A2E"
    canvas: str = "横長 3:2"


D = Defaults()


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("hint")
    lab.setWordWrap(True)
    return lab


def _slider(lo: int, hi: int, val: int) -> QSlider:
    s = QSlider(Qt.Orientation.Horizontal)
    s.setRange(lo, hi)
    s.setValue(val)
    return s


def _spin(lo: int, hi: int, val: int, suffix: str = "") -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(val)
    s.setSuffix(suffix)
    s.setAccelerated(True)
    return s


class MainWindow(QMainWindow):
    def __init__(self, check_updates_on_start: bool = True):
        super().__init__()
        self.settings = QSettings(config.APP_ID, config.APP_ID)
        self.setWindowTitle(f"{config.APP_NAME}　{VERSION}")
        icon = paths.asset("app.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.setMinimumSize(1100, 680)
        self.resize(1360, 820)
        self.setStyleSheet(STYLESHEET)

        self._main_photo: image_io.LoadedImage | None = None
        self._text_job = 0                 # 文字プレビューの「何回目の依頼か」（古い結果を捨てるため）
        self._last_text_result = None
        self._fonts: tuple[text_mask.FontInfo, ...] = ()
        self._active_workers: set[workers.Worker] = set()
        self._result: mosaic_engine.MosaicResult | None = None   # 最後に生成したモザイク
        self._result_mode = -1
        self._gen_worker: workers.Worker | None = None
        self._anim_renderer: animation.AnimationRenderer | None = None
        self._anim_key = None
        self._anim_frame = 0
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._anim_tick)
        self._anim_busy = False

        self._text_timer = QTimer(self, singleShot=True, interval=350)
        self._text_timer.timeout.connect(self._render_text_preview)

        self._build_menu()
        self._build_ui()
        self._restore_settings()

        self._load_fonts()
        self.setAcceptDrops(True)
        self._drop_leave_timer = QTimer(self, singleShot=True, interval=120)
        self._drop_leave_timer.timeout.connect(self._end_drag_hint)
        QApplication.instance().installEventFilter(self)  # 画面全体のドラッグ＆ドロップを受け取る
        if check_updates_on_start:  # 前回使った写真を自動で読み込む（解析結果の保存庫があるので速い）
            QTimer.singleShot(600, self.collection_panel.restore_previous)
        self._update_photo_summary()
        self._on_mode_changed(self.mode_tabs.currentIndex())

        if check_updates_on_start and self.act_auto_update.isChecked() and updater.updates_supported():
            QTimer.singleShot(2500, lambda: self.check_updates(manual=False))

    # ==================================================================
    # 画面の組み立て
    # ==================================================================
    def _build_menu(self) -> None:
        m_file = self.menuBar().addMenu("ファイル(&F)")
        a = QAction("メイン写真を選択…", self, shortcut=QKeySequence("Ctrl+O"))
        a.triggered.connect(self.choose_main_photo)
        m_file.addAction(a)
        a = QAction("タイル用写真のフォルダーを追加…", self, shortcut=QKeySequence("Ctrl+Shift+O"))
        a.triggered.connect(lambda: self.collection_panel.choose_folder())
        m_file.addAction(a)
        a = QAction("タイル用の写真ファイルを追加…", self, shortcut=QKeySequence("Ctrl+Shift+I"))
        a.triggered.connect(lambda: self.collection_panel.choose_files())
        m_file.addAction(a)
        m_file.addSeparator()
        a = QAction("終了", self, shortcut=QKeySequence("Ctrl+Q"))
        a.triggered.connect(self.close)
        m_file.addAction(a)

        m_help = self.menuBar().addMenu("ヘルプ(&H)")
        a = QAction("更新を確認…", self)
        a.triggered.connect(lambda: self.check_updates(manual=True))
        m_help.addAction(a)
        self.act_auto_update = QAction("起動時に更新を確認する", self, checkable=True)
        self.act_auto_update.setChecked(self.settings.value("update/auto_check", True, type=bool))
        self.act_auto_update.toggled.connect(lambda v: self.settings.setValue("update/auto_check", v))
        m_help.addAction(self.act_auto_update)
        m_help.addSeparator()
        a = QAction("このアプリについて", self)
        a.triggered.connect(self._about)
        m_help.addAction(a)

    def _build_ui(self) -> None:
        central = QWidget(objectName="central")
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(8)

        # ---- タイトル ----
        head = QHBoxLayout()
        title = QLabel(config.APP_NAME, objectName="appTitle")
        chip = QLabel(f"ver {VERSION}", objectName="versionChip")
        head.addWidget(title)
        head.addWidget(chip)
        head.addStretch(1)
        root.addLayout(head)

        # ---- ① 写真コレクション ----
        self.collection_panel = CollectionPanel(self.settings)
        self.collection_panel.message.connect(lambda t, ms: self.statusBar().showMessage(t, ms))
        self.collection_panel.changed.connect(lambda n: self._update_generate_enabled())
        root.addWidget(self.collection_panel)

        # ---- ② 設定 ＋ ③ プレビュー ----
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        left = QScrollArea()
        left.setWidgetResizable(True)
        left.setFrameShape(QFrame.Shape.NoFrame)
        left.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left.setMinimumWidth(470)
        self.mode_tabs = QTabWidget()
        self.mode_tabs.addTab(self._build_photo_tab(), "写真モザイク")
        self.mode_tabs.addTab(self._build_text_tab(), "文字モザイク")
        self.mode_tabs.currentChanged.connect(self._on_mode_changed)
        left.setWidget(self.mode_tabs)
        self.splitter.addWidget(left)
        self.splitter.addWidget(self._build_preview_panel())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([500, 860])
        root.addWidget(self.splitter, 1)

        # ---- 生成・保存 ----
        root.addWidget(self._build_bottom_bar())
        self.setCentralWidget(central)
        self.statusBar().showMessage("準備ができました。")

    # ---------------- 写真モザイク ----------------
    def _build_photo_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)

        g1 = QGroupBox("メイン写真（モザイクで再現する写真）")
        l1 = QVBoxLayout(g1)
        row = QHBoxLayout()
        self.btn_main = QPushButton("🖼 メイン写真を選択")
        self.btn_main.clicked.connect(self.choose_main_photo)
        self.lbl_main = QLabel("まだ選ばれていません")
        self.lbl_main.setObjectName("hint")
        self.lbl_main.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.btn_main)
        row.addWidget(self.lbl_main, 1)
        l1.addLayout(row)
        v.addWidget(g1)

        g2 = QGroupBox("タイルの設定")
        f = QFormLayout(g2)
        self.sp_tile = _spin(10, 200, D.tile_px, " px")
        self.sp_cols = _spin(10, 400, D.photo_cols, " 枚")
        self.sp_rows = _spin(10, 400, 75, " 枚")
        self.chk_aspect = QCheckBox("メイン写真の縦横比を保つ")
        self.chk_aspect.setChecked(True)
        f.addRow("タイル1枚の大きさ", self.sp_tile)
        f.addRow("横のタイル数", self.sp_cols)
        f.addRow("縦のタイル数", self.sp_rows)
        f.addRow("", self.chk_aspect)
        for s in (self.sp_tile, self.sp_cols, self.sp_rows):
            s.valueChanged.connect(self._update_photo_summary)
        self.chk_aspect.toggled.connect(self._update_photo_summary)
        v.addWidget(g2)

        g3 = QGroupBox("仕上がりの調整")
        f3 = QFormLayout(g3)
        self.sp_uses = _spin(1, 200, D.max_uses, " 回まで")
        self.sl_match = _slider(0, 100, D.color_match)
        self.sl_edge = _slider(0, 100, D.edge)
        self.chk_border = QCheckBox("タイルの境界線を表示")
        f3.addRow("同じ写真の使用回数", self.sp_uses)
        f3.addRow("色の一致度", self.sl_match)
        f3.addRow("輪郭の強調", self.sl_edge)
        f3.addRow("", self.chk_border)
        f3.addRow(_hint("色の一致度を上げると元の写真に近づき、下げると多くの写真が使われます。"))
        v.addWidget(g3)
        v.addStretch(1)
        return w

    # ---------------- 文字モザイク ----------------
    def _build_text_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)

        g1 = QGroupBox("文章（1〜40文字）")
        l1 = QVBoxLayout(g1)
        self.ed_text = QPlainTextEdit()
        self.ed_text.setPlaceholderText("例：DayDream Plus 2027 ありがとう")
        self.ed_text.setFixedHeight(76)
        self.ed_text.setTabChangesFocus(True)
        self.ed_text.textChanged.connect(self._on_text_changed)
        row = QHBoxLayout()
        self.lbl_counter = QLabel("0 / 40文字", objectName="counter")
        row.addWidget(_hint("Enterキーで改行できます（複数行のとき）"), 1)
        row.addWidget(self.lbl_counter)
        l1.addWidget(self.ed_text)
        l1.addLayout(row)
        v.addWidget(g1)

        g2 = QGroupBox("文字のデザイン")
        f = QFormLayout(g2)
        self.cb_font = QComboBox()
        self.cb_font.addItem("フォントを探しています…")
        self.cb_font.setEnabled(False)
        self.cb_font.currentIndexChanged.connect(self._schedule_text_preview)
        self.sl_weight = _slider(0, 10, D.weight)
        self.chk_autosize = QCheckBox("自動（いちばん大きく収まるサイズ）")
        self.chk_autosize.setChecked(True)
        self.sp_fontsize = _spin(20, 2000, 300, " px")
        self.sp_fontsize.setEnabled(False)
        self.chk_autosize.toggled.connect(lambda on: self.sp_fontsize.setEnabled(not on))
        self.sl_spacing = _slider(-10, 100, D.letter_spacing)
        self.sl_linesp = _slider(80, 250, D.line_spacing)
        layout_row = QHBoxLayout()
        self.rb_multi = QRadioButton("複数行（自動で折り返す）")
        self.rb_single = QRadioButton("1行")
        self.rb_multi.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_multi)
        grp.addButton(self.rb_single)
        layout_row.addWidget(self.rb_multi)
        layout_row.addWidget(self.rb_single)
        layout_row.addStretch(1)
        f.addRow("フォント", self.cb_font)
        f.addRow("文字の太さ", self.sl_weight)
        f.addRow("文字サイズ", self.chk_autosize)
        f.addRow("", self.sp_fontsize)
        f.addRow("文字間隔", self.sl_spacing)
        f.addRow("行間", self.sl_linesp)
        f.addRow("並べ方", layout_row)
        v.addWidget(g2)

        g3 = QGroupBox("色とキャンバス")
        f3 = QFormLayout(g3)
        self.btn_text_color = QPushButton(objectName="colorSwatch")
        self.btn_bg_color = QPushButton(objectName="colorSwatch")
        self._text_color, self._bg_color = QColor(D.text_color), QColor(D.bg_color)
        self.btn_text_color.clicked.connect(lambda: self._pick_color("text"))
        self.btn_bg_color.clicked.connect(lambda: self._pick_color("bg"))
        self._refresh_swatches()
        bg_row = QHBoxLayout()
        self.rb_bg_solid = QRadioButton("単色")
        self.rb_bg_photo = QRadioButton("写真タイル")
        self.rb_bg_solid.setChecked(True)
        self.rb_bg_photo.setToolTip("背景も写真で埋めます（モザイク生成機能と同時に対応予定）")
        g_bg = QButtonGroup(self)
        g_bg.addButton(self.rb_bg_solid)
        g_bg.addButton(self.rb_bg_photo)
        bg_row.addWidget(self.btn_bg_color)
        bg_row.addWidget(self.rb_bg_solid)
        bg_row.addWidget(self.rb_bg_photo)
        bg_row.addStretch(1)
        self.cb_canvas = QComboBox()
        self.cb_canvas.addItems(list(CANVAS_PRESETS))
        f3.addRow("文字の色（写真が少ない部分の補助色）", self.btn_text_color)
        f3.addRow("背景", bg_row)
        f3.addRow("キャンバスの形", self.cb_canvas)
        v.addWidget(g3)

        g4 = QGroupBox("タイル")
        f4 = QFormLayout(g4)
        self.chk_auto_tiles = QCheckBox("文字が読める細かさを自動で選ぶ（おすすめ）")
        self.chk_auto_tiles.setChecked(True)
        self.sp_text_cols = _spin(20, 400, 120, " 枚")
        self.sp_text_cols.setEnabled(False)
        self.chk_auto_tiles.toggled.connect(lambda on: self.sp_text_cols.setEnabled(not on))
        self.sp_text_tile = _spin(10, 200, D.tile_px, " px")
        f4.addRow("", self.chk_auto_tiles)
        f4.addRow("横のタイル数", self.sp_text_cols)
        f4.addRow("タイル1枚の大きさ", self.sp_text_tile)
        v.addWidget(g4)

        # ---- 🎬 文字が集まるアニメーション ----
        g5 = QGroupBox("🎬 写真が集まって文字になるアニメーション（200種類）")
        f5 = QFormLayout(g5)
        row5 = QHBoxLayout()
        self.cb_anim = QComboBox()
        self.cb_anim.addItems([p.name for p in animation.PRESETS])
        self.cb_anim.setMaxVisibleItems(20)
        # 長い名前でも欄が横に広がりすぎないようにする（一覧を開くと全文が見える）
        self.cb_anim.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cb_anim.setMinimumContentsLength(16)
        self.cb_anim.view().setMinimumWidth(420)
        self.btn_anim_random = QPushButton("🎲 おまかせ")
        self.btn_anim_random.setToolTip("200種類の中からランダムに選びます")
        self.btn_anim_random.clicked.connect(self._random_animation)
        row5.addWidget(self.cb_anim, 1)
        row5.addWidget(self.btn_anim_random)
        f5.addRow("動き方", row5)
        self.cb_anim_size = QComboBox()
        for label, wh in (("1080×1920（縦・ショート動画）", (1080, 1920)), ("720×1280（縦・軽量）", (720, 1280)),
                          ("1920×1080（横・YouTube）", (1920, 1080)), ("1080×1080（正方形・Instagram）", (1080, 1080))):
            self.cb_anim_size.addItem(label, wh)
        f5.addRow("動画の大きさ", self.cb_anim_size)
        row6 = QHBoxLayout()
        self.sp_anim_sec = _spin(2, 20, 5, " 秒")
        self.sp_anim_hold = _spin(0, 10, 2, " 秒")
        row6.addWidget(self.sp_anim_sec)
        row6.addWidget(QLabel("　完成後に止める"))
        row6.addWidget(self.sp_anim_hold)
        row6.addStretch(1)
        f5.addRow("集まる時間", row6)
        self.cb_anim_fps = QComboBox()
        self.cb_anim_fps.addItem("30 fps（標準）", 30)
        self.cb_anim_fps.addItem("60 fps（なめらか）", 60)
        f5.addRow("なめらかさ", self.cb_anim_fps)
        row7 = QHBoxLayout()
        self.btn_anim_play = QPushButton("▶ プレビュー再生")
        self.btn_anim_play.clicked.connect(self.toggle_animation_preview)
        self.btn_anim_export = QPushButton("🎞 動画を書き出す")
        self.btn_anim_export.setToolTip("MP4 動画・GIF アニメ・PNG 連番から選べます")
        self.btn_anim_export.clicked.connect(self.export_animation)
        row7.addWidget(self.btn_anim_play)
        row7.addWidget(self.btn_anim_export, 1)
        f5.addRow(row7)
        f5.addRow(_hint("文字・フォント・色・背景は上の設定がそのまま使われます。\nタイルの細かさは動画の大きさに合わせて自動で決まります。"))
        v.addWidget(g5)

        self.lbl_text_warn = QLabel("", objectName="warn")
        self.lbl_text_warn.setWordWrap(True)
        v.addWidget(self.lbl_text_warn)
        v.addStretch(1)

        for sig in (self.sl_weight.valueChanged, self.sl_spacing.valueChanged, self.sl_linesp.valueChanged,
                    self.sp_fontsize.valueChanged, self.chk_autosize.toggled, self.rb_multi.toggled,
                    self.cb_canvas.currentIndexChanged, self.chk_auto_tiles.toggled,
                    self.sp_text_cols.valueChanged, self.sp_text_tile.valueChanged):
            sig.connect(self._schedule_text_preview)
        return w

    # ---------------- プレビュー ----------------
    def _build_preview_panel(self) -> QWidget:
        box = QGroupBox("③ プレビュー")
        v = QVBoxLayout(box)
        bar = QHBoxLayout()
        self.tb_before = QToolButton(text="生成前", checkable=True, checked=True)
        self.tb_after = QToolButton(text="生成後", checkable=True)
        self.tb_after.setEnabled(False)
        self.tb_after.setToolTip("モザイクを生成すると見られます")
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        grp.addButton(self.tb_before)
        grp.addButton(self.tb_after)
        bar.addWidget(self.tb_before)
        bar.addWidget(self.tb_after)
        self.tb_before.clicked.connect(self._show_before)
        self.tb_after.clicked.connect(self._show_after)
        bar.addStretch(1)
        self.lbl_zoom = QLabel("", objectName="hint")
        b_out = QToolButton(text="－")
        b_fit = QToolButton(text="全体表示")
        b_in = QToolButton(text="＋")
        bar.addWidget(self.lbl_zoom)
        bar.addWidget(b_out)
        bar.addWidget(b_fit)
        bar.addWidget(b_in)
        v.addLayout(bar)
        self.preview = PreviewView()
        self.preview.zoomChanged.connect(lambda z: self.lbl_zoom.setText(f"{z * 100:.0f}%"))
        b_out.clicked.connect(lambda: self.preview.zoom(1 / 1.25))
        b_in.clicked.connect(lambda: self.preview.zoom(1.25))
        b_fit.clicked.connect(self.preview.fit)
        v.addWidget(self.preview, 1)
        self.lbl_preview_info = _hint("")
        v.addWidget(self.lbl_preview_info)
        return box

    def _build_bottom_bar(self) -> QWidget:
        w = QFrame()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        self.lbl_summary = QLabel("", objectName="summary")
        self.btn_reset = QPushButton("設定を初期化")
        self.btn_reset.clicked.connect(self.reset_settings)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(180)
        self.progress.setVisible(False)
        self.btn_generate = QPushButton("✨ モザイク生成開始", objectName="primary")
        self.btn_generate.setEnabled(False)
        self.btn_generate.clicked.connect(self.generate)
        self.btn_cancel_gen = QPushButton("中止")
        self.btn_cancel_gen.setVisible(False)
        self.btn_cancel_gen.clicked.connect(self.cancel_generation)
        self.btn_save = QPushButton("💾 保存（PNG／JPEG）")
        self.btn_save.setEnabled(False)
        self.btn_save.setToolTip("モザイクを生成すると保存できます")
        self.btn_save.clicked.connect(self.save_result)
        h.addWidget(self.lbl_summary, 1)
        h.addWidget(self.btn_reset)
        h.addWidget(self.progress)
        h.addWidget(self.btn_cancel_gen)
        h.addWidget(self.btn_generate)
        h.addWidget(self.btn_save)
        return w

    # ==================================================================
    # 写真モザイク
    # ==================================================================
    def choose_main_photo(self) -> None:
        start = self.settings.value("paths/main_photo_dir", str(Path.home() / "Pictures"))
        f, _ = QFileDialog.getOpenFileName(self, "メイン写真を選択", start,
                                           "写真 (*.jpg *.jpeg *.jfif *.png *.JPG *.JPEG *.JFIF *.PNG);;すべてのファイル (*)")
        if not f:
            return
        self.settings.setValue("paths/main_photo_dir", str(Path(f).parent))
        self.load_main_photo(f)

    def load_main_photo(self, f: str) -> None:
        self.mode_tabs.setCurrentIndex(0)
        self.lbl_main.setText("読み込み中…")
        self._run(lambda: image_io.load_image(f, max_side=PREVIEW_MAX), self._on_main_loaded)

    def _on_main_loaded(self, li: image_io.LoadedImage) -> None:
        self._main_photo = li
        w, h = li.original_size
        self.lbl_main.setText(f"{li.path.name}（{w:,}×{h:,}）")
        self.lbl_main.setToolTip(str(li.path))
        if li.warning:
            self.statusBar().showMessage(li.warning, 10000)
        self._update_photo_summary()
        self._update_generate_enabled()
        if self.mode_tabs.currentIndex() == 0:
            self.tb_before.setChecked(True)
            self.preview.set_image(li.image)
            self.lbl_preview_info.setText("生成前：メイン写真")

    def _update_photo_summary(self) -> None:
        if self._main_photo and self.chk_aspect.isChecked():
            w, h = self._main_photo.original_size
            rows = max(10, round(self.sp_cols.value() * h / w))
            self.sp_rows.blockSignals(True)
            self.sp_rows.setValue(min(400, rows))
            self.sp_rows.blockSignals(False)
        self.sp_rows.setEnabled(not (self._main_photo and self.chk_aspect.isChecked()))
        if self.mode_tabs.currentIndex() == 0:
            self._show_summary(self.sp_cols.value(), self.sp_rows.value(), self.sp_tile.value())

    def _show_summary(self, cols: int, rows: int, tile: int) -> None:
        w, h = cols * tile, rows * tile
        mem_mb = w * h * 3 * 2 / 1024 / 1024  # 作業用に約2枚分
        text = f"出力 {w:,}×{h:,}px　タイル {cols}×{rows}＝{cols * rows:,}枚　必要メモリ 約{mem_mb:,.0f}MB"
        if w * h > config.MAX_OUTPUT_PIXELS:
            text += "　⚠ 大きすぎます。タイル数か大きさを減らしてください"
        self.lbl_summary.setText(text)

    # ==================================================================
    # 文字モザイク
    # ==================================================================
    def _on_text_changed(self) -> None:
        txt = self.ed_text.toPlainText()
        if text_mask.count_chars(txt) > config.TEXT_MAX_CHARS:
            # 41文字目以降は受け付けない（カーソル位置を保ったまま切り詰める）
            cur = self.ed_text.textCursor()
            pos = cur.position()
            trimmed = text_mask.truncate_to_limit(txt)
            self.ed_text.blockSignals(True)
            self.ed_text.setPlainText(trimmed)
            self.ed_text.blockSignals(False)
            cur = self.ed_text.textCursor()
            cur.setPosition(min(pos, len(trimmed)))
            self.ed_text.setTextCursor(cur)
            self.statusBar().showMessage(f"{config.TEXT_MAX_CHARS}文字を超える入力はできません。", 4000)
            txt = trimmed
        n = text_mask.count_chars(txt)
        self.lbl_counter.setText(f"{n} / {config.TEXT_MAX_CHARS}文字")
        self.lbl_counter.setProperty("over", n >= config.TEXT_MAX_CHARS)
        self.lbl_counter.style().unpolish(self.lbl_counter)
        self.lbl_counter.style().polish(self.lbl_counter)
        self._schedule_text_preview()
        if hasattr(self, "btn_generate"):
            self._update_generate_enabled()

    def _load_fonts(self) -> None:
        self._run(text_mask.find_japanese_fonts, self._on_fonts_loaded, on_error=self._on_fonts_failed)

    def _on_fonts_loaded(self, fonts) -> None:
        self._fonts = fonts
        self.cb_font.blockSignals(True)
        self.cb_font.clear()
        if not fonts:
            self.cb_font.addItem("日本語フォントが見つかりません")
            self.cb_font.blockSignals(False)
            return
        for f in fonts:
            self.cb_font.addItem(f.display_name)
        saved = self.settings.value("text/font", "")
        try:
            default = text_mask.default_font().display_name
        except MosaicError:
            default = fonts[0].display_name
        idx = self.cb_font.findText(saved or default)
        self.cb_font.setCurrentIndex(max(0, idx))
        self.cb_font.setEnabled(True)
        self.cb_font.blockSignals(False)
        self._schedule_text_preview()

    def _on_fonts_failed(self, e) -> None:
        self.cb_font.clear()
        self.cb_font.addItem("フォントを読み込めませんでした")

    def _current_font(self) -> text_mask.FontInfo | None:
        i = self.cb_font.currentIndex()
        return self._fonts[i] if self._fonts and 0 <= i < len(self._fonts) else None

    def _pick_color(self, which: str) -> None:
        cur = self._text_color if which == "text" else self._bg_color
        c = QColorDialog.getColor(cur, self, "文字の色" if which == "text" else "背景の色")
        if not c.isValid():
            return
        if which == "text":
            self._text_color = c
        else:
            self._bg_color = c
        self._refresh_swatches()
        self._schedule_text_preview()

    def _refresh_swatches(self) -> None:
        for btn, c in ((self.btn_text_color, self._text_color), (self.btn_bg_color, self._bg_color)):
            btn.setStyleSheet(f"background:{c.name()}; border:1px solid #B9B0D0; border-radius:6px;")
            btn.setToolTip(c.name().upper())

    def _canvas_size(self) -> tuple[int, int]:
        rw, rh = CANVAS_PRESETS.get(self.cb_canvas.currentText(), (3, 2))
        if rw >= rh:
            return MASK_LONG_SIDE, round(MASK_LONG_SIDE * rh / rw)
        return round(MASK_LONG_SIDE * rw / rh), MASK_LONG_SIDE

    def _text_settings(self) -> text_mask.TextMaskSettings:
        return text_mask.TextMaskSettings(
            font=self._current_font(),
            canvas_size=self._canvas_size(),
            weight=self.sl_weight.value(),
            letter_spacing=self.sl_spacing.value() / 100,
            line_spacing=self.sl_linesp.value() / 100,
            multiline=self.rb_multi.isChecked(),
            font_size=None if self.chk_autosize.isChecked() else self.sp_fontsize.value(),
        )

    def _schedule_text_preview(self, *_) -> None:
        if self.mode_tabs.currentIndex() == 1:
            self._text_timer.start()

    @staticmethod
    def build_text_preview(text, settings, auto_tiles, manual_cols, text_rgb, bg_rgb):
        """（作業係で実行）文字マスクを作り、タイルの升目にした見え方の画像を作る。"""
        res = text_mask.render_text_mask(text, settings)
        cw, ch = res.mask.size
        if auto_tiles:
            cols, rows = text_mask.auto_grid(res)
        else:
            cols = manual_cols
            rows = max(1, round(cols * ch / cw))
        cell = max(2, PREVIEW_MAX // max(cols, rows))
        img = text_mask.grid_preview(res.mask, cols, rows, cell=cell, text_color=text_rgb, bg_color=bg_rgb)
        return res, cols, rows, img

    def _render_text_preview(self) -> None:
        text = self.ed_text.toPlainText()
        if text_mask.count_chars(text_mask.normalize_text(text)) == 0:
            self.preview.set_image(None)
            self.preview.set_placeholder("文章を入力すると、ここに完成イメージが表示されます")
            self.lbl_text_warn.setText("")
            self.lbl_summary.setText("文章を入力してください")
            return
        if not self._fonts:
            return
        self._text_job += 1
        job = self._text_job
        args = (text, self._text_settings(), self.chk_auto_tiles.isChecked(), self.sp_text_cols.value(),
                self._text_color.getRgb()[:3], self._bg_color.getRgb()[:3])
        self.lbl_preview_info.setText("プレビューを作成中…")
        self._run(lambda: self.build_text_preview(*args),
                  lambda r: self._on_text_preview(job, r),
                  on_error=lambda e: self._on_text_preview_error(job, e))

    def _on_text_preview(self, job: int, result) -> None:
        if job != self._text_job or self.mode_tabs.currentIndex() != 1:
            return  # もっと新しい依頼があるので、この結果は使わない
        res, cols, rows, img = result
        self._last_text_result = result
        if self.tb_before.isChecked():
            self.preview.set_image(img, keep_zoom=True)
        lines = " ／ ".join(res.lines)
        self.lbl_preview_info.setText(f"文字の配置：{lines}　（{res.font.display_name}・{res.font_size}px相当）")
        self.lbl_text_warn.setText("\n".join("⚠ " + w for w in res.warnings))
        if self.chk_auto_tiles.isChecked():
            self.sp_text_cols.blockSignals(True)
            self.sp_text_cols.setValue(min(400, cols))
            self.sp_text_cols.blockSignals(False)
        elif res.font_size / max(1, res.mask.width / cols) < text_mask.MIN_TILES_PER_CHAR:
            self.lbl_text_warn.setText(self.lbl_text_warn.text() +
                                       ("\n" if self.lbl_text_warn.text() else "") +
                                       "⚠ タイルが粗いため、画数の多い漢字が読みにくい可能性があります。横のタイル数を増やしてください。")
        self._show_summary(cols, rows, self.sp_text_tile.value())
        self._update_generate_enabled()

    def _on_text_preview_error(self, job: int, e) -> None:
        if job != self._text_job:
            return
        msg = e.user_message() if isinstance(e, MosaicError) else f"プレビューを作成できませんでした：{e}"
        self.lbl_text_warn.setText("⚠ " + msg.replace("\n\n", "　"))
        self.lbl_preview_info.setText("")

    # ==================================================================
    # モード切り替え・設定の保存
    # ==================================================================
    def _on_mode_changed(self, idx: int) -> None:
        if hasattr(self, "_anim_timer") and self._anim_timer.isActive():
            self._anim_timer.stop()
            self._set_anim_buttons()
        self.tb_before.setChecked(True)
        self.tb_after.setEnabled(self._result is not None and self._result_mode == idx)
        self.btn_save.setEnabled(self._result is not None and self._result_mode == idx)
        self._update_generate_enabled()
        if idx == 0:
            if self._main_photo:
                self.preview.set_image(self._main_photo.image)
                self.lbl_preview_info.setText("生成前：メイン写真")
            else:
                self.preview.set_image(None)
                self.preview.set_placeholder("「メイン写真を選択」を押すか、ここに写真を1枚ドラッグしてください")
                self.lbl_preview_info.setText("")
            self._update_photo_summary()
        else:
            self.preview.set_image(None)
            self.preview.set_placeholder("プレビューを作成中…")
            self._render_text_preview()

    def _restore_settings(self) -> None:
        s = self.settings
        self.ed_text.setPlainText(s.value("text/text", D.text))
        self._text_color = QColor(s.value("text/color", D.text_color))
        self._bg_color = QColor(s.value("text/bg", D.bg_color))
        self._refresh_swatches()
        self.sl_weight.setValue(int(s.value("text/weight", D.weight)))
        self.sl_spacing.setValue(int(s.value("text/spacing", D.letter_spacing)))
        self.sl_linesp.setValue(int(s.value("text/linesp", D.line_spacing)))
        (self.rb_multi if s.value("text/multiline", True, type=bool) else self.rb_single).setChecked(True)
        i = self.cb_canvas.findText(s.value("text/canvas", D.canvas))
        self.cb_canvas.setCurrentIndex(max(0, i))
        self.mode_tabs.setCurrentIndex(int(s.value("ui/mode", 0)))
        geo = s.value("ui/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("text/text", self.ed_text.toPlainText())
        s.setValue("text/color", self._text_color.name())
        s.setValue("text/bg", self._bg_color.name())
        s.setValue("text/weight", self.sl_weight.value())
        s.setValue("text/spacing", self.sl_spacing.value())
        s.setValue("text/linesp", self.sl_linesp.value())
        s.setValue("text/multiline", self.rb_multi.isChecked())
        s.setValue("text/canvas", self.cb_canvas.currentText())
        if self._current_font():
            s.setValue("text/font", self._current_font().display_name)
        s.setValue("ui/mode", self.mode_tabs.currentIndex())
        s.setValue("ui/geometry", self.saveGeometry())

    def reset_settings(self) -> None:
        r = QMessageBox.question(self, "設定を初期化",
                                 "すべての設定を最初の状態に戻します。よろしいですか？\n（写真や保存した画像は消えません）")
        if r != QMessageBox.StandardButton.Yes:
            return
        self.sp_tile.setValue(D.tile_px)
        self.sp_cols.setValue(D.photo_cols)
        self.sp_uses.setValue(D.max_uses)
        self.sl_match.setValue(D.color_match)
        self.sl_edge.setValue(D.edge)
        self.chk_border.setChecked(False)
        self.chk_aspect.setChecked(True)
        self.sl_weight.setValue(D.weight)
        self.sl_spacing.setValue(D.letter_spacing)
        self.sl_linesp.setValue(D.line_spacing)
        self.chk_autosize.setChecked(True)
        self.rb_multi.setChecked(True)
        self.rb_bg_solid.setChecked(True)
        self.chk_auto_tiles.setChecked(True)
        self.sp_text_tile.setValue(D.tile_px)
        self.cb_canvas.setCurrentIndex(0)
        self._text_color, self._bg_color = QColor(D.text_color), QColor(D.bg_color)
        self._refresh_swatches()
        try:
            idx = self.cb_font.findText(text_mask.default_font().display_name)
            if idx >= 0:
                self.cb_font.setCurrentIndex(idx)
        except MosaicError:
            pass
        self._update_photo_summary()
        self._schedule_text_preview()
        self.statusBar().showMessage("設定を初期化しました。", 5000)

    def closeEvent(self, e) -> None:
        if self.collection_panel.is_busy():
            self.collection_panel.cancel()  # 読み込み中に閉じても安全に止める
        if self._gen_worker:
            self._gen_worker.cancel()
        self._anim_timer.stop()
        QApplication.instance().removeEventFilter(self)
        self._save_settings()
        for w in list(self._active_workers):
            w.cancel()
        super().closeEvent(e)

    # ==================================================================
    # モザイク生成・プレビュー切り替え・保存
    # ==================================================================
    def _generate_blocker(self) -> str:
        """生成できない理由（できるときは空文字）。"""
        if self._gen_worker:
            return "生成中です"
        if len(self.collection_panel.collection) == 0:
            return "① 写真コレクションに、タイル用の写真を追加してください"
        if self.mode_tabs.currentIndex() == 0:
            if self._main_photo is None:
                return "メイン写真を選んでください"
        else:
            n = text_mask.count_chars(text_mask.normalize_text(self.ed_text.toPlainText()))
            if n < config.TEXT_MIN_CHARS:
                return "文章を入力してください"
            if not self._fonts:
                return "フォントを準備しています"
        return ""

    def _update_generate_enabled(self) -> None:
        reason = self._generate_blocker()
        self.btn_generate.setEnabled(not reason)
        self.btn_generate.setToolTip(reason or "モザイクを作ります（途中で中止できます）")

    def generate(self) -> None:
        if self._generate_blocker():
            return
        records = list(self.collection_panel.collection.records)
        mode = self.mode_tabs.currentIndex()
        seed = int.from_bytes(__import__("os").urandom(4), "little")
        if mode == 0:
            s = mosaic_engine.PhotoMosaicSettings(
                cols=self.sp_cols.value(), rows=self.sp_rows.value(), tile_px=self.sp_tile.value(),
                max_uses=self.sp_uses.value(), color_match=self.sl_match.value(), edge=self.sl_edge.value(),
                border=self.chk_border.isChecked(), seed=seed)
            fn, args = mosaic_engine.generate_photo_mosaic, (str(self._main_photo.path), records, s)
        else:
            text = self.ed_text.toPlainText()
            ms = self._text_settings()
            auto, manual = self.chk_auto_tiles.isChecked(), self.sp_text_cols.value()
            tile_px = self.sp_text_tile.value()
            text_rgb, bg_rgb = self._text_color.getRgb()[:3], self._bg_color.getRgb()[:3]
            bg_photos = self.rb_bg_photo.isChecked()

            def fn(progress=None, is_cancelled=None):
                _, cols, rows, _ = MainWindow.build_text_preview(text, ms, auto, manual, text_rgb, bg_rgb)
                st = mosaic_engine.TextMosaicSettings(cols=cols, rows=rows, tile_px=tile_px, text_color=text_rgb,
                                                      bg_color=bg_rgb, bg_photos=bg_photos, seed=seed)
                return mosaic_engine.generate_text_mosaic(text, ms, records, st, progress, is_cancelled)
            args = ()

        w = workers.Worker(fn, *args, with_progress=True)
        w.signals.progress.connect(self._on_gen_progress)
        self._gen_worker = w
        self._set_generating(True)
        self._track(w, lambda r: self._on_generated(r, mode), self._on_generate_failed)
        workers.start(w)

    def _set_generating(self, on: bool) -> None:
        self.progress.setVisible(on)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.btn_cancel_gen.setVisible(on)
        self.btn_cancel_gen.setEnabled(True)
        self.btn_generate.setText("生成中…" if on else "✨ モザイク生成開始")
        self.mode_tabs.setEnabled(not on)
        self.btn_reset.setEnabled(not on)
        self._update_generate_enabled()
        if on:
            self.statusBar().showMessage("モザイクを作っています…（写真の枚数やタイル数によって数十秒〜数分かかります）")

    def _on_gen_progress(self, done: int, total: int) -> None:
        self.progress.setValue(int(done * 1000 / max(1, total)))

    def cancel_generation(self) -> None:
        if self._gen_worker:
            self._gen_worker.cancel()
            self.btn_cancel_gen.setEnabled(False)
            self.statusBar().showMessage("中止しています…")

    def _on_generated(self, result: mosaic_engine.MosaicResult, mode: int) -> None:
        self._gen_worker = None
        self._set_generating(False)
        self._result, self._result_mode = result, mode
        self.mode_tabs.setCurrentIndex(mode)
        self.tb_after.setEnabled(True)
        self.btn_save.setEnabled(True)
        self._show_after()
        self.tb_after.setChecked(True)
        self.statusBar().showMessage(f"完成しました（{result.stats.get('生成時間', '')}）。「保存」で画像を保存できます。", 15000)
        if result.warnings:
            QMessageBox.information(self, "お知らせ", "\n\n".join(result.warnings))

    def _on_generate_failed(self, e) -> None:
        self._gen_worker = None
        self._set_generating(False)
        if isinstance(e, mosaic_engine.GenerationCancelled):
            self.statusBar().showMessage("生成を中止しました。", 6000)
            return
        self._show_error(e)

    def _show_before(self) -> None:
        self.tb_before.setChecked(True)
        if self.mode_tabs.currentIndex() == 0:
            if self._main_photo:
                self.preview.set_image(self._main_photo.image)
                self.lbl_preview_info.setText("生成前：メイン写真")
        elif self._last_text_result:
            self.preview.set_image(self._last_text_result[3])
            self.lbl_preview_info.setText("生成前：文字の配置（タイルの升目）")

    def _show_after(self) -> None:
        if not self._result or self._result_mode != self.mode_tabs.currentIndex():
            return
        self.tb_after.setChecked(True)
        self.preview.set_image(self._result.preview)
        self.lbl_preview_info.setText("生成後：" + "　".join(f"{k} {v}" for k, v in self._result.stats.items()))

    def save_result(self) -> None:
        if not self._result:
            return
        mode = "text" if self._result_mode == 1 else "photo"
        dlg = SaveDialog(self.settings, self._result.image.size, mode, self)
        if dlg.exec() != SaveDialog.DialogCode.Accepted:
            return
        protected = {r.path for r in self.collection_panel.collection.records}
        if self._main_photo:
            protected.add(str(self._main_photo.path))
        img, settings, opts = self._result.image, self._result.settings, dlg.options
        self.statusBar().showMessage("保存しています…")
        self.btn_save.setEnabled(False)

        def done(res: exporter.SaveResult):
            self.btn_save.setEnabled(True)
            self.statusBar().showMessage(f"保存しました：{res.path}", 15000)
            box = QMessageBox(self)
            box.setWindowTitle("保存しました")
            box.setIcon(QMessageBox.Icon.Information)
            msg = (f"保存先：{res.path}\n大きさ：{res.width:,}×{res.height:,}px（{res.bytes / 1e6:,.1f}MB）")
            if res.settings_path:
                msg += f"\n設定情報：{Path(res.settings_path).name}"
            box.setText(msg)
            b_open = box.addButton("フォルダーを開く", QMessageBox.ButtonRole.ActionRole)
            box.addButton("閉じる", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is b_open:
                from PySide6.QtCore import QUrl
                from PySide6.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(res.path).parent)))

        def failed(e):
            self.btn_save.setEnabled(True)
            self._show_error(e)

        self._run(lambda: exporter.save_image(img, opts, protected, settings), done, on_error=failed)

    # ==================================================================
    # 🎬 アニメーション
    # ==================================================================
    def _random_animation(self) -> None:
        import random
        self.cb_anim.setCurrentIndex(random.randrange(len(animation.PRESETS)))

    def _anim_settings(self, seed: int = 1) -> tuple:
        w, h = self.cb_anim_size.currentData()
        st = animation.AnimationSettings(width=w, height=h, fps=self.cb_anim_fps.currentData(),
                                         duration=float(self.sp_anim_sec.value()),
                                         hold=float(self.sp_anim_hold.value()),
                                         preset=self.cb_anim.currentIndex(), seed=seed)
        ms = self._text_settings()
        text = self.ed_text.toPlainText()
        key = (text, w, h, ms.font.path if ms.font else "", ms.weight, ms.letter_spacing, ms.line_spacing,
               ms.multiline, ms.font_size, self._text_color.name(), self._bg_color.name(),
               self.rb_bg_photo.isChecked(), len(self.collection_panel.collection),
               tuple(r.sha1 for r in self.collection_panel.collection.records[:50]))
        return text, ms, st, key

    def _with_renderer(self, then) -> None:
        """アニメーション係を用意してから then(renderer) を呼ぶ（写真の読み込みは作業係で）。"""
        reason = self._generate_blocker() if self.mode_tabs.currentIndex() == 1 else ""
        if reason and reason != "生成中です":
            QMessageBox.information(self, "アニメーション", reason)
            return
        text, ms, st, key = self._anim_settings()
        if self._anim_renderer is not None and self._anim_key == key:
            # 文字・写真・動画サイズが同じなら、写真を読み直さずに動き方・長さだけ反映
            self._anim_renderer = self._anim_renderer.with_settings(st)
            then(self._anim_renderer)
            return
        records = list(self.collection_panel.collection.records)
        text_rgb, bg_rgb = self._text_color.getRgb()[:3], self._bg_color.getRgb()[:3]
        bg_photos = self.rb_bg_photo.isChecked()
        self._anim_busy = True
        self._set_anim_buttons()
        self.statusBar().showMessage("アニメーションの準備をしています（写真を読み込み中）…")

        def build(progress=None, is_cancelled=None):
            return animation.build_renderer(text, ms, records, text_rgb, bg_rgb, bg_photos, st,
                                            progress, is_cancelled)

        w = workers.Worker(build, with_progress=True)
        w.signals.progress.connect(lambda d, t: self.statusBar().showMessage(
            f"アニメーションの準備をしています… {int(d * 100 / max(1, t))}%"))

        def done(r):
            self._anim_busy = False
            self._anim_renderer, self._anim_key = r, key
            self._set_anim_buttons()
            self.statusBar().clearMessage()
            then(r)

        def failed(e):
            self._anim_busy = False
            self._set_anim_buttons()
            self._show_error(e)

        self._track(w, done, failed)
        workers.start(w)

    def _set_anim_buttons(self) -> None:
        playing = self._anim_timer.isActive()
        self.btn_anim_play.setText("⏹ 停止" if playing else "▶ プレビュー再生")
        self.btn_anim_play.setEnabled(not self._anim_busy)
        self.btn_anim_export.setEnabled(not self._anim_busy and not playing)

    def toggle_animation_preview(self) -> None:
        if self._anim_timer.isActive():
            self.stop_animation_preview()
            return

        def start(r):
            self._anim_frame = 0
            self.tb_before.setChecked(False)
            self.tb_after.setChecked(False)
            self._anim_timer.start(int(1000 / r.st.fps))
            self.lbl_preview_info.setText(f"アニメーション：{r.preset.name}")
            self._set_anim_buttons()

        self._with_renderer(start)

    def stop_animation_preview(self) -> None:
        self._anim_timer.stop()
        self._set_anim_buttons()
        self._show_before()

    def _anim_tick(self) -> None:
        r = self._anim_renderer
        if r is None or self.mode_tabs.currentIndex() != 1:
            self.stop_animation_preview()
            return
        vp = self.preview.viewport().size()
        scale = min(1.0, max(0.15, min(vp.width() / r.W, vp.height() / r.H)))
        self.preview.set_qimage(r.frame(self._anim_frame, scale))
        self._anim_frame = (self._anim_frame + 1) % r.frame_count  # 最後まで行ったら最初から繰り返す

    def export_animation(self) -> None:
        if self._anim_timer.isActive():
            self.stop_animation_preview()
        dlg = AnimationExportDialog(self.settings, video_export.mp4_codec_name(), self)
        if dlg.exec() != AnimationExportDialog.DialogCode.Accepted:
            return
        fmt, path = dlg.result_format, dlg.result_path
        self._with_renderer(lambda r: self._run_export(r, fmt, path))

    def _run_export(self, r: animation.AnimationRenderer, fmt: str, path: str) -> None:
        prog = QProgressDialog("動画を書き出しています…", "中止", 0, 1000, self)
        prog.setWindowTitle("動画の書き出し")
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)
        prog.setValue(0)
        cancelled = {"v": False}
        prog.canceled.connect(lambda: cancelled.update(v=True))

        def report(d, t):
            prog.setValue(int(d * 1000 / max(1, t)))

        def finished(msg: str, folder: str):
            prog.reset()
            box = QMessageBox(self)
            box.setWindowTitle("書き出しました")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(msg)
            b_open = box.addButton("フォルダーを開く", QMessageBox.ButtonRole.ActionRole)
            box.addButton("閉じる", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is b_open:
                from PySide6.QtCore import QUrl
                from PySide6.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

        def failed(e):
            prog.reset()
            if isinstance(e, video_export.ExportCancelled):
                self.statusBar().showMessage("書き出しを中止しました。", 6000)
            else:
                self._show_error(e)

        secs = r.frame_count / r.st.fps
        if fmt == "mp4":  # 動画機能は画面と同じスレッドで動かす（進み具合は表示される）
            try:
                out, codec = video_export.export_mp4(r, path, report, lambda: cancelled["v"])
            except Exception as e:
                failed(e)
                return
            size = Path(out).stat().st_size / 1e6
            finished(f"MP4 動画を書き出しました。\n\n保存先：{out}\n大きさ：{r.W}×{r.H}・{secs:.1f}秒・{size:.1f}MB\n"
                     f"圧縮方式：{codec}\n動き方：{r.preset.name}", str(Path(out).parent))
            return

        if fmt == "gif":
            fn = lambda progress=None, is_cancelled=None: video_export.export_gif(r, path, progress=progress, is_cancelled=is_cancelled)  # noqa: E731
        else:
            fn = lambda progress=None, is_cancelled=None: video_export.export_png_sequence(r, path, progress, is_cancelled)  # noqa: E731
        w = workers.Worker(fn, with_progress=True)
        w.signals.progress.connect(report)
        prog.canceled.connect(w.cancel)

        def done(res):
            if fmt == "gif":
                finished(f"GIF アニメを書き出しました。\n\n保存先：{res}\n大きさ：{Path(res).stat().st_size / 1e6:.1f}MB\n"
                         f"動き方：{r.preset.name}", str(Path(res).parent))
            else:
                folder, n = res
                finished(f"PNG連番を書き出しました（{n}枚・{r.st.fps}fps）。\n\n保存先：{folder}\n"
                         "動画編集ソフトで「画像シーケンス」として読み込めます。", folder)

        self._track(w, done, failed)
        workers.start(w)

    # ==================================================================
    # ドラッグ＆ドロップ
    #   画面のどの部品（サムネイル一覧・プレビュー・入力欄・数値欄など）の上に落としても、
    #   アプリ全体で受け取って振り分ける。文字入力欄にファイル名が入ってしまうこともない。
    # ==================================================================
    _DRAG_TYPES = (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop, QEvent.Type.DragLeave)

    @staticmethod
    def _local_paths(mime) -> list[str]:
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile()] if mime and mime.hasUrls() else []

    def _drop_goes_to_main_photo(self, widget: QWidget, pos, paths: list[str]) -> bool:
        """写真モザイクのプレビュー欄に写真を1枚だけ落としたときは、メイン写真にする。"""
        if self.mode_tabs.currentIndex() != 0 or len(paths) != 1:
            return False
        p = Path(paths[0])
        if not (p.is_file() and p.suffix.lower() in config.CANDIDATE_EXTENSIONS):
            return False
        vp = self.preview.viewport()
        local = vp.mapFromGlobal(widget.mapToGlobal(pos))
        return vp.rect().contains(local)

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() not in self._DRAG_TYPES or not isinstance(obj, QWidget) or obj.window() is not self:
            return super().eventFilter(obj, ev)
        if ev.type() == QEvent.Type.DragLeave:
            self._drop_leave_timer.start()  # 部品の間を移動しただけなら、すぐ次の DragEnter が来る
            return False
        paths = self._local_paths(ev.mimeData())
        if not paths:
            return False  # 写真ファイル以外（文字など）は通常どおり
        pos = ev.position().toPoint()
        to_main = self._drop_goes_to_main_photo(obj, pos, paths)
        if ev.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            ev.setDropAction(Qt.DropAction.CopyAction)
            ev.accept()
            self._drop_leave_timer.stop()
            self.collection_panel.set_drop_highlight(not to_main, len(paths))
            self.statusBar().showMessage("離すと「メイン写真」になります" if to_main else
                                         f"離すと、タイル用の写真に追加されます（{len(paths)}個）")
            return True
        # Drop
        ev.setDropAction(Qt.DropAction.CopyAction)
        ev.accept()
        self.collection_panel.set_drop_highlight(False)
        self.statusBar().clearMessage()
        if to_main:
            self.load_main_photo(paths[0])
        else:
            self.collection_panel.add_sources(paths)
        return True

    def _end_drag_hint(self) -> None:
        self.collection_panel.set_drop_highlight(False)
        self.statusBar().clearMessage()

    # ==================================================================
    # 自動更新
    # ==================================================================
    def check_updates(self, manual: bool) -> None:
        if not updater.updates_supported():
            if manual:
                QMessageBox.information(self, "更新の確認",
                                        "この版は開発用のため、自動更新は使えません。\n"
                                        "インストーラーから入れたアプリで利用できます。")
            return
        if manual:
            self.statusBar().showMessage("更新を確認しています…")

        def done(info):
            self.statusBar().clearMessage()
            if info is None:
                if manual:
                    QMessageBox.information(self, "更新の確認", f"お使いの版（{VERSION}）が最新です。")
                return
            self._offer_update(info)

        self._run(updater.check_for_update, done,
                  on_error=lambda e: manual and QMessageBox.warning(self, "更新の確認", f"確認できませんでした：{e}"))

    def _offer_update(self, info: updater.UpdateInfo) -> None:
        notes = info.notes[:600] + ("…" if len(info.notes) > 600 else "")
        box = QMessageBox(self)
        box.setWindowTitle("新しいバージョンがあります")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"新しいバージョン {info.version} が公開されています。\n（現在：{VERSION}）\n\n今すぐ更新しますか？")
        if notes:
            box.setDetailedText(notes)
        b_yes = box.addButton("今すぐ更新", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("後で", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not b_yes:
            return

        dlg = QProgressDialog("更新ファイルをダウンロードしています…", "中止", 0, 100, self)
        dlg.setWindowTitle("更新")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        worker = workers.Worker(updater.download_update, info, with_progress=True)
        dlg.canceled.connect(worker.cancel)

        def on_progress(done, total):
            if total:
                dlg.setValue(int(done * 100 / total))

        def on_done(path):
            dlg.reset()
            QMessageBox.information(self, "更新",
                                    "更新の準備ができました。\nアプリを一度閉じて、新しいバージョンに入れ替えます。\n"
                                    "完了すると自動でアプリが起動します。")
            self._save_settings()
            try:
                updater.launch_installer(path)
            except Exception as e:
                QMessageBox.warning(self, "更新", f"更新を開始できませんでした：{e}")
                return
            QApplication.quit()

        def on_err(e):
            dlg.reset()
            if not worker.cancelled:
                QMessageBox.warning(self, "更新できませんでした", str(e))

        worker.signals.progress.connect(on_progress)
        self._track(worker, on_done, on_err)
        workers.start(worker)

    # ==================================================================
    # その他
    # ==================================================================
    def _about(self) -> None:
        QMessageBox.about(self, "このアプリについて",
                          f"<b>{config.APP_NAME}</b><br>バージョン {VERSION}<br><br>"
                          "写真はすべてこのパソコンの中だけで処理され、外部に送信されません。<br>"
                          "更新の確認では、新しいバージョンの有無だけを確認します。<br><br>"
                          "© DayDream AI株式会社")

    def _run(self, fn, on_done, on_error=None) -> workers.Worker:
        w = workers.Worker(fn)
        self._track(w, on_done, on_error or self._show_error)
        return workers.start(w)

    def _track(self, w: workers.Worker, on_done, on_error) -> None:
        self._active_workers.add(w)

        def fin(r):
            self._active_workers.discard(w)
            on_done(r)

        def err(e):
            self._active_workers.discard(w)
            on_error(e)

        w.signals.finished.connect(fin)
        w.signals.failed.connect(err)

    def _show_error(self, e) -> None:
        if isinstance(e, MosaicError):
            QMessageBox.warning(self, "お知らせ", e.user_message())
        elif isinstance(e, MemoryError):
            QMessageBox.critical(self, "メモリ不足", "メモリが足りません。他のアプリを閉じてから、もう一度お試しください。")
        else:
            QMessageBox.critical(self, "エラー", f"思わぬエラーが起きました。\n\n{e}")
        self.statusBar().clearMessage()
        if self.lbl_main.text() == "読み込み中…":
            self.lbl_main.setText("読み込めませんでした")
