# PyInstaller の設定：Python がなくても動くアプリ（EXE＋部品フォルダー）にまとめる。
# Qt の部品（DLL）を EXE に埋め込まずフォルダーに分けて同梱する「onedir」形式。
# → 起動が速く、PySide6（LGPL）のライセンス条件にも沿った配布方法になる。
import os

version_file = os.path.join("build", "version_info.txt")

a = Analysis(
    ["main.py"],
    pathex=[],
    datas=[("assets", "assets"), ("THIRD_PARTY_NOTICES.txt", ".")],
    hiddenimports=[],
    excludes=["tkinter", "unittest.mock", "pydoc", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtWebEngineCore", "PySide6.QtMultimedia", "PySide6.Qt3DCore"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DayDreamPlusDigitalMosaic",
    console=False,          # 黒い画面（コンソール）を出さない
    icon="assets/app.ico",
    version=version_file if os.path.exists(version_file) else None,
    upx=False,              # 圧縮するとウイルス対策ソフトに誤検知されやすいため使わない
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="DayDreamPlusDigitalMosaic",
)
