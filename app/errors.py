"""アプリ独自のエラー（例外）の定義。

どのエラーにも「原因」と「対処方法」を日本語で持たせ、
画面にそのまま表示できるようにしています。
"""


class MosaicError(Exception):
    """このアプリで起きるエラーの共通の親。"""

    def __init__(self, cause: str, remedy: str = ""):
        super().__init__(cause)
        self.cause = cause
        self.remedy = remedy

    def user_message(self) -> str:
        if self.remedy:
            return f"{self.cause}\n\n【対処方法】{self.remedy}"
        return self.cause


class ImageLoadError(MosaicError):
    """画像が読み込めないとき（破損・非対応形式・巨大すぎる等）。"""


class TextInputError(MosaicError):
    """文字モザイクの入力文章に問題があるとき。"""


class FontError(MosaicError):
    """フォントが見つからない・日本語を表示できないとき。"""
