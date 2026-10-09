"""自動更新：GitHub に公開された最新版を確認し、セットアップEXEで上書き更新する。

流れ
  1. GitHub の「最新リリース」情報だけを取得（写真などのデータは一切送らない）
  2. 今のバージョンより新しければ、利用者に「更新しますか？」と確認
  3. セットアップEXEをダウンロードし、一緒に公開した SHA256（指紋のような値）と照合
     → 1文字でも違えば改ざん・破損とみなして実行しない
  4. セットアップを「静かなモード」で起動し、アプリを閉じる
     → インストール完了後、新しいアプリが自動で起動する

オフラインのときや GitHub に接続できないときは、何も表示せず普段どおり使える。
この通信は「起動時に更新を確認する」をオフにすれば行われない。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config
from .version import IS_RELEASE_BUILD, REPO, VERSION

# ダウンロードを許可する接続先（これ以外の場所からは取得しない）
_ALLOWED_HOSTS = ("https://github.com/", "https://api.github.com/",
                  "https://objects.githubusercontent.com/",
                  "https://release-assets.githubusercontent.com/")


class UpdateError(Exception):
    """更新に失敗したとき。メッセージは日本語で画面に出せる。"""


@dataclass
class UpdateInfo:
    version: str
    notes: str
    setup_url: str
    sha256_url: str
    size: int


# --------------------------------------------------------------------------
# バージョン比較
# --------------------------------------------------------------------------
def parse_version(v: str) -> tuple[int, ...]:
    """'v0.2.15' や '0.2.15-dev' を (0, 2, 15) に変換する。読めなければ (0,)。"""
    m = re.match(r"^v?(\d+(?:\.\d+)*)", v.strip())
    if not m:
        return (0,)
    parts = tuple(int(x) for x in m.group(1).split("."))
    return parts + (0,) * (3 - len(parts)) if len(parts) < 3 else parts


def is_newer(remote: str, current: str) -> bool:
    return parse_version(remote) > parse_version(current)


# --------------------------------------------------------------------------
# 最新版の確認
# --------------------------------------------------------------------------
def _request(url: str, timeout: float):
    if not url.startswith(_ALLOWED_HOSTS):
        raise UpdateError("許可されていない場所からのダウンロードを中止しました。")
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{config.APP_ID}/{VERSION}",
        "Accept": "application/vnd.github+json",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def parse_release(data: dict) -> UpdateInfo | None:
    """GitHub のリリース情報から、必要なファイルのURLを取り出す。"""
    if data.get("draft") or data.get("prerelease"):
        return None
    assets = {a.get("name"): a for a in data.get("assets", [])}
    setup = assets.get(config.SETUP_ASSET_NAME)
    sha = assets.get(config.SETUP_ASSET_NAME + ".sha256")
    if not setup or not sha:
        return None
    return UpdateInfo(
        version=str(data.get("tag_name", "")).lstrip("v"),
        notes=str(data.get("body") or "").strip(),
        setup_url=setup["browser_download_url"],
        sha256_url=sha["browser_download_url"],
        size=int(setup.get("size") or 0),
    )


def check_for_update(repo: str = REPO, current: str = VERSION,
                     timeout: float = config.UPDATE_CHECK_TIMEOUT) -> UpdateInfo | None:
    """新しいバージョンがあれば UpdateInfo、なければ None。接続できない場合も None。"""
    if not repo:
        return None
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        with _request(url, timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None  # オフライン・まだリリースが無い等は「更新なし」と同じ扱い
    info = parse_release(data)
    if info and is_newer(info.version, current):
        return info
    return None


# --------------------------------------------------------------------------
# ダウンロードと検証
# --------------------------------------------------------------------------
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_sha256_file(text: str) -> str:
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not m:
        raise UpdateError("更新ファイルの確認用データが読み取れませんでした。")
    return m.group(1).lower()


def download_update(info: UpdateInfo,
                    progress: Callable[[int, int], None] | None = None,
                    is_cancelled: Callable[[], bool] | None = None,
                    timeout: float = 30) -> Path:
    """セットアップEXEをダウンロードし、SHA256 を照合してから保存場所を返す。"""
    try:
        with _request(info.sha256_url, timeout) as r:
            expected = parse_sha256_file(r.read().decode("utf-8", "replace"))

        dest_dir = Path(tempfile.mkdtemp(prefix="ddp_mosaic_update_"))
        dest = dest_dir / config.SETUP_ASSET_NAME
        with _request(info.setup_url, timeout) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or info.size or 0)
            done = 0
            while True:
                if is_cancelled and is_cancelled():
                    raise UpdateError("更新を中止しました。")
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except UpdateError:
        raise
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"更新ファイルをダウンロードできませんでした。\nインターネット接続を確認してください。（{e}）")

    if sha256_of(dest) != expected:
        try:
            dest.unlink()
        except OSError:
            pass
        raise UpdateError("ダウンロードした更新ファイルが正しくありません（破損または改ざんの可能性）。\n安全のため更新を中止しました。")
    return dest


def launch_installer(setup_path: Path) -> None:
    """セットアップを静かなモードで起動する。呼び出し側はこのあとアプリを終了すること。"""
    if not sys.platform.startswith("win"):
        raise UpdateError("自動更新は Windows でのみ利用できます。")
    args = [str(setup_path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CLOSEAPPLICATIONS"]  # 完了後の再起動はインストーラー側で行う
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, close_fds=True, creationflags=flags, cwd=os.path.dirname(setup_path))


def updates_supported() -> bool:
    """自動ビルドで作られた Windows 用のアプリのときだけ自動更新を有効にする。"""
    return IS_RELEASE_BUILD and getattr(sys, "frozen", False) and sys.platform.startswith("win")
