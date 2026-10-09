"""アプリ全体で使う定数（設定値）をまとめたファイル。

数値を変えたいときは、ここだけを修正すれば全体に反映されます。
"""

APP_NAME = "DayDream Plus デジタルモザイク"
APP_ID = "DayDreamPlusDigitalMosaic"
APP_VERSION = "0.1.0"  # フェーズ1

# ---- 写真コレクション ----
MAX_TILE_PHOTOS = 2000

# 拡張子は「候補を絞る目安」にだけ使い、実際の判定はファイルの中身で行う
CANDIDATE_EXTENSIONS = {".jpg", ".jpeg", ".jfif", ".png"}

# Pillow が中身から判定する形式名。JFIF は JPEG の一種なので "JPEG" と判定される
ACCEPTED_FORMATS = {"JPEG", "PNG"}

# 1枚あたり何画素まで開くか（巨大画像・悪意ある画像からPCを守る上限）
# 例：1億画素 ≒ 12,000 × 8,000 程度
MAX_INPUT_PIXELS = 100_000_000

# 透過PNGをタイルに使うときに合成する背景色（白）
DEFAULT_ALPHA_BACKGROUND = (255, 255, 255)

# ---- 文字モザイク ----
TEXT_MIN_CHARS = 1
TEXT_MAX_CHARS = 40

# ---- 出力 ----
DEFAULT_JPEG_QUALITY = 92
# 出力画像の上限（メモリ保護）。RGBで約 3 バイト/画素
MAX_OUTPUT_PIXELS = 400_000_000  # 例：20,000 × 20,000
