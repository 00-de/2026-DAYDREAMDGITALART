"""自動ビルド用：バージョン番号をアプリに書き込む。

使い方（GitHub が自動で実行します）：
    python tools/write_build_info.py 0.2.15 owner/repo
作るファイル：
    app/_build_info.py      … アプリが自分のバージョンと更新確認先を知るため
    build/version_info.txt  … EXE のプロパティ（右クリック→詳細）に出る情報
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    version, repo = sys.argv[1], sys.argv[2]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"バージョン番号の形式が正しくありません: {version}")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        sys.exit(f"リポジトリ名の形式が正しくありません: {repo}")

    (ROOT / "app" / "_build_info.py").write_text(
        "# 自動ビルドで生成されたファイルです（手で編集しないでください）\n"
        f'VERSION = "{version}"\nREPO = "{repo}"\n', encoding="utf-8")

    nums = tuple(int(x) for x in version.split(".")) + (0,)
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041104B0', [
      StringStruct('CompanyName', 'DayDream AI株式会社'),
      StringStruct('FileDescription', 'DayDream Plus デジタルモザイク'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'DayDreamPlusDigitalMosaic'),
      StringStruct('OriginalFilename', 'DayDreamPlusDigitalMosaic.exe'),
      StringStruct('ProductName', 'DayDream Plus デジタルモザイク'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [0x0411, 1200])])
  ]
)
""", encoding="utf-8")
    print(f"バージョン {version}（{repo}）を書き込みました")


if __name__ == "__main__":
    main()
