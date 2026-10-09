"""開発環境チェック：必要な部品がそろっているか、日本語フォントがあるかを確認する。"""

import platform
import sys


def main() -> int:
    print("=== DayDream Plus デジタルモザイク　環境チェック ===")
    print(f"OS     : {platform.platform()}")
    print(f"Python : {sys.version.split()[0]}")
    ok = True
    for mod, label in (("PySide6", "PySide6（画面）"), ("PIL", "Pillow（画像）"),
                       ("numpy", "NumPy（計算）"), ("regex", "regex（文字数）")):
        try:
            m = __import__(mod)
            print(f"  OK  {label:<18} {getattr(m, '__version__', '')}")
        except ImportError:
            print(f"  NG  {label:<18} 見つかりません")
            ok = False
    if not ok:
        print("\n必要な部品が足りません。check_env.bat をもう一度実行してください。")
        return 1

    from app import text_mask
    fonts = text_mask.find_japanese_fonts()
    print(f"\n日本語フォント：{len(fonts)}個見つかりました")
    for f in fonts[:8]:
        print(f"  ・{f.display_name}")
    print(f"初期フォント：{text_mask.default_font().display_name}")
    print("\nすべて正常です。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
