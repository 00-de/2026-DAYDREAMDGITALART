"""画面の見た目（色・角丸・余白）。DayDream Plus らしい紫→ピンクのアクセント。"""

ACCENT = "#7C4DFF"
ACCENT2 = "#FF4F9A"

STYLESHEET = f"""
QMainWindow, QWidget#central {{ background: #F6F4FB; }}
QLabel#appTitle {{ font-size: 18px; font-weight: 700; color: #2B2340; }}
QLabel#versionChip {{
    background: #ECE6FF; color: {ACCENT}; border-radius: 9px; padding: 2px 10px; font-weight: 600;
}}
QGroupBox {{
    background: #FFFFFF; border: 1px solid #E3DEF0; border-radius: 10px;
    margin-top: 14px; padding: 12px 10px 10px 10px; font-weight: 600; color: #2B2340;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; }}
QTabWidget::pane {{ border: 1px solid #E3DEF0; border-radius: 10px; background: #FFFFFF; top: -1px; }}
QTabBar::tab {{
    background: #ECE8F6; color: #4A4060; padding: 8px 18px; margin-right: 4px;
    border-top-left-radius: 8px; border-top-right-radius: 8px; font-weight: 600;
}}
QTabBar::tab:selected {{ background: #FFFFFF; color: {ACCENT}; }}
QPushButton {{
    background: #FFFFFF; border: 1px solid #D6CFE8; border-radius: 8px; padding: 6px 14px; color: #2B2340;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #A9A3B8; background: #F1EFF6; border-color: #E6E2EF; }}
QPushButton#primary {{
    color: white; font-weight: 700; font-size: 15px; padding: 10px 26px; border: none;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2});
}}
QPushButton#primary:disabled {{ background: #CFC8E3; color: #F7F5FC; }}
QPushButton#colorSwatch {{ min-width: 46px; max-width: 46px; min-height: 22px; padding: 0; }}
QPlainTextEdit, QLineEdit {{
    background: #FFFFFF; border: 1px solid #D6CFE8; border-radius: 6px; padding: 4px;
}}
QPlainTextEdit:focus, QLineEdit:focus {{ border-color: {ACCENT}; }}
QSlider::groove:horizontal {{ height: 6px; background: #E6E1F3; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{
    background: white; border: 2px solid {ACCENT}; width: 14px; height: 14px; margin: -6px 0; border-radius: 9px;
}}
QProgressBar {{ border: none; background: #E6E1F3; border-radius: 6px; height: 12px; text-align: center; }}
QProgressBar::chunk {{
    border-radius: 6px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2});
}}
QGraphicsView#previewView {{ background: #2A2638; border: none; border-radius: 10px; }}
QLabel#hint {{ color: #7A7290; font-size: 12px; }}
QLabel#warn {{ color: #B23A62; font-size: 12px; }}
QLabel#summary {{ color: #2B2340; font-weight: 600; }}
QLabel#counter {{ font-weight: 700; color: {ACCENT}; }}
QLabel#counter[over="true"] {{ color: #D2335C; }}
QToolButton {{ border: 1px solid #D6CFE8; border-radius: 6px; padding: 4px 10px; background: #FFFFFF; }}
QToolButton:checked {{ background: #ECE6FF; border-color: {ACCENT}; color: {ACCENT}; font-weight: 700; }}
"""
