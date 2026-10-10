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
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
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
            cache = collection.AnalysisCache(d / "c.sqlite")
            recs = collection.analyze_sources([str(tiles)], cache=cache).records
            cache.close()  # Windows では開いたままのファイルを削除できないため閉じる
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

        # 4) 集合アニメーション → MP4（どの圧縮方式が使えるかを記録。失敗しても GIF／PNG は使える）
        try:
            from . import animation, video_export
            if fonts:
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d2:
                    d2 = Path(d2)
                    tiles2 = d2 / "t"
                    tiles2.mkdir()
                    for i in range(8):
                        Image.new("RGB", (30, 30), (200, 100 + i * 15, 255)).save(tiles2 / f"{i}.jpg", "JPEG")
                    c2 = collection.AnalysisCache(d2 / "c.sqlite")
                    recs2 = collection.analyze_sources([str(tiles2)], cache=c2).records
                    c2.close()
                    st = animation.AnimationSettings(width=180, height=320, fps=10, duration=1, hold=0.2, preset=42)
                    r = animation.build_renderer("DD", text_mask.TextMaskSettings(), recs2, (255, 255, 255),
                                                 (20, 20, 40), False, st)
                    path, codec = video_export.export_mp4(r, str(d2 / "a"))
                    checks["animation_presets"] = len(animation.PRESETS)
                    checks["mp4"] = f"{codec} ({Path(path).stat().st_size} bytes)"
            else:
                checks["mp4"] = "skipped: no Japanese font"
        except Exception as e:  # MP4 が作れなくてもアプリ自体は使えるので、記録だけする
            checks["mp4"] = f"error: {e}"

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
