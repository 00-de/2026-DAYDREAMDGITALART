"""DayDream Plus デジタルモザイク　起動用プログラム。"""

from __future__ import annotations

import sys


def main() -> int:
    # 自己診断モード（自動ビルドの検証用）
    if "--self-test" in sys.argv:
        i = sys.argv.index("--self-test")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest_result.json"
        import os
        # 画面を実際には出さずに診断する（使えない環境では通常の画面表示に切り替え）
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen;windows" if sys.platform.startswith("win") else "offscreen")
        from app.selftest import run
        return run(out)

    if sys.platform.startswith("win"):
        try:  # タスクバーにこのアプリのアイコンを正しく出すための設定
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DayDreamAI.DigitalMosaic")
        except Exception:
            pass

    from PySide6.QtWidgets import QApplication, QMessageBox

    from app import config
    from app.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setOrganizationName(config.APP_ID)
    app.setStyle("Fusion")

    def excepthook(exc_type, exc, tb):  # 予想外のエラーでもアプリを落とさず日本語で知らせる
        import traceback
        detail = "".join(traceback.format_exception(exc_type, exc, tb))[-1500:]
        QMessageBox.critical(None, "エラー", f"思わぬエラーが起きました。\n\n{exc}\n\n---\n{detail}")
    sys.excepthook = excepthook

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
