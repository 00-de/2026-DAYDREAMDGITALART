"""自己診断：EXE化・インストール後に、アプリの部品が正しく動くかを自動で確認する。

GitHub の自動ビルドで「インストール → 起動 → この診断 → アンインストール」を毎回行い、
壊れたインストーラーが公開されないようにしています。
使い方：DayDreamPlusDigitalMosaic.exe --self-test 結果.json
"""

from __future__ import annotations

import json
import platform
import sys
import tempfile
import time
import traceback
from pathlib import Path


def run(result_path: str) -> int:
    report: dict = {"ok": False, "checks": {}, "python": sys.version.split()[0],
                    "os": platform.platform(), "frozen": bool(getattr(sys, "frozen", False))}
    checks = report["checks"]
    t0 = time.time()
    try:
        from PIL import Image

        from . import image_io, text_mask
        from .version import VERSION
        report["version"] = VERSION

        # 1) JPG / JFIF / PNG（透過）の読み込み
        with tempfile.TemporaryDirectory() as d:
            d = Path(d) / "自己診断"
            d.mkdir()
            Image.new("RGB", (64, 48), (200, 40, 60)).save(d / "テスト.jfif", "JPEG")
            Image.new("RGB", (64, 48), (40, 200, 60)).save(d / "test.jpg", "JPEG")
            Image.new("RGBA", (64, 48), (0, 0, 255, 0)).save(d / "alpha.png")
            (d / "broken.jpg").write_bytes(b"\xff\xd8\xff\xe0broken")
            ok = sum(1 for f in image_io.iter_candidate_files(d) if _try_load(image_io, f))
            checks["image_io"] = ok == 3

            # 1b) 写真コレクション → モザイク生成 → 保存
            from . import collection, exporter, mosaic_engine
            tiles = d / "タイル"
            tiles.mkdir()
            for i in range(12):
                Image.new("RGB", (40, 30), (i * 20, 255 - i * 20, 128)).save(tiles / f"t{i}.jpg", "JPEG")
            recs = collection.analyze_sources([str(tiles)], cache=collection.AnalysisCache(d / "c.sqlite")).records
            res = mosaic_engine.generate_photo_mosaic(
                str(d / "テスト.jfif"), recs, mosaic_engine.PhotoMosaicSettings(cols=12, rows=9, tile_px=8))
            saved = exporter.save_image(res.image, exporter.SaveOptions(str(d / "完成"), "JPEG", 90))
            checks["mosaic"] = len(recs) == 12 and Path(saved.path).stat().st_size > 0

        # 2) 日本語フォントと文字マスク
        fonts = text_mask.find_japanese_fonts()
        checks["japanese_fonts"] = len(fonts)
        if fonts:
            r = text_mask.render_text_mask("DayDream Plus 垂井町", text_mask.TextMaskSettings(canvas_size=(800, 500)))
            checks["text_mask"] = bool(r.as_array().max() == 255)
        else:
            checks["text_mask"] = "skipped: no Japanese font on this machine"

        # 3) 画面（ウィンドウを作って表示できるか）
        from PySide6.QtWidgets import QApplication

        from .ui.main_window import MainWindow
        app = QApplication.instance() or QApplication(sys.argv[:1])
        win = MainWindow(check_updates_on_start=False)
        win.show()
        app.processEvents()
        checks["gui"] = win.isVisible()
        win.close()
        app.processEvents()

        report["ok"] = (checks["image_io"] is True and checks["mosaic"] is True and checks["gui"] is True
                        and checks["text_mask"] is not False)
    except Exception:
        report["error"] = traceback.format_exc()
    report["seconds"] = round(time.time() - t0, 2)
    Path(result_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


def _try_load(image_io, f) -> bool:
    try:
        image_io.load_image(f, max_side=32)
        return True
    except Exception:
        return False
