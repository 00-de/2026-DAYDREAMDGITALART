"""バージョン番号の管理。

番号のしくみ（例：0.2.15）
  0.2  … 大きな区切り。フェーズが進んだときなどに、このファイルの BASE_VERSION を手で変える
  15   … GitHub で自動ビルドされた回数。コードを送るたびに自動で1ずつ増える（自動バージョンアップ）

自動ビルドのときは、GitHub が _build_info.py を作って本当の番号とリポジトリ名を書き込みます。
自分のPCでそのまま動かしたときは「開発版」になり、自動更新は動きません。
"""

BASE_VERSION = "0.3"

try:
    from ._build_info import REPO, VERSION  # type: ignore  # 自動ビルド時に生成される
except ImportError:
    VERSION = f"{BASE_VERSION}.0-dev"
    REPO = ""

IS_RELEASE_BUILD = bool(REPO) and not VERSION.endswith("-dev")
