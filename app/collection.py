"""写真コレクション：タイル用の写真（最大2,000枚）を読み込み・検査・解析する。

しくみ
  1. 追加されたフォルダー／ファイルから候補を集める（対象外の拡張子は除外）
  2. 解析結果の保存庫（キャッシュ）を確認：前回と同じファイルなら解析を省略して即座に使う
  3. 残りを複数の作業係で同時に解析（CPUのコア数に応じて並列処理）
       - ファイルを1回だけ読み、その中身から「指紋（SHA-1）」と画像の両方を作る
       - 指紋が同じ＝中身が同じ写真 → 重複として除外（ファイル名が違っても検出できる）
       - 壊れた写真は「失敗」に記録して、残りの処理を続ける
  4. 色の特徴（平均色・区画ごとの色）と小さなサムネイルだけを記憶する
     → 2,000枚でもメモリをほとんど使わない（元の写真は生成時に必要な分だけ読み直す）

画面から独立した部品なので、作業係（スレッド）の中で安全に呼び出せる。
"""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
from PIL import Image

from . import config, image_io, paths
from .color import image_features
from .errors import ImageLoadError

ANALYSIS_SIDE = 128   # 解析用に縮小する長辺（px）
FEATURE_SIDE = 32     # 色の計算に使う大きさ
THUMB_SIDE = 96       # サムネイルの長辺
MAX_FILE_BYTES = 200 * 1024 * 1024  # 1ファイル200MBを超えるものは写真として扱わない
CACHE_VERSION = 1     # 解析方法を変えたら上げる（古い解析結果を使わないため）


@dataclass
class PhotoRecord:
    path: str
    sha1: str
    width: int
    height: int
    mean_rgb: list[float]
    mean_lab: list[float]
    grid_lab: list[float]
    thumb_jpeg: bytes
    warning: str = ""


@dataclass
class BatchResult:
    records: list[PhotoRecord] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)   # (パス, 理由)
    duplicates: list[str] = field(default_factory=list)
    ignored: int = 0            # 対象外の拡張子
    already: int = 0            # すでに登録済みだった写真
    over_limit: int = 0         # 上限2,000枚を超えたため追加しなかった枚数
    from_cache: int = 0
    cancelled: bool = False
    seconds: float = 0.0


# --------------------------------------------------------------------------
# 候補ファイルの収集
# --------------------------------------------------------------------------
def expand_sources(sources: Iterable[str], recursive: bool) -> tuple[list[Path], int]:
    """フォルダーとファイルが混ざった指定から、候補ファイルの一覧を作る。"""
    out: list[Path] = []
    seen: set[str] = set()
    ignored = 0
    for s in sources:
        p = Path(s)
        if p.is_dir():
            items = list(image_io.iter_candidate_files(p, recursive=recursive))
        elif p.is_file():
            if p.suffix.lower() not in config.CANDIDATE_EXTENSIONS:
                ignored += 1
                continue
            items = [p]
        else:
            continue
        for f in items:
            k = _norm(f)
            if k not in seen:
                seen.add(k)
                out.append(f)
    return out, ignored


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


# --------------------------------------------------------------------------
# 解析結果の保存庫（キャッシュ）
# --------------------------------------------------------------------------
class AnalysisCache:
    """解析結果を %LOCALAPPDATA%\\DayDreamPlusDigitalMosaic\\analysis_cache.sqlite に保存する。

    「ファイルの場所・大きさ・更新日時」が前回と同じなら、写真を開かずに結果を再利用する。
    """

    def __init__(self, db_path: Path | None = None):
        self.path = db_path or (paths.user_data_dir() / "analysis_cache.sqlite")
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("""CREATE TABLE IF NOT EXISTS photos (
            key TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, ver INTEGER,
            sha1 TEXT, width INTEGER, height INTEGER,
            mean_rgb BLOB, mean_lab BLOB, grid_lab BLOB, thumb BLOB, warning TEXT)""")
        self.db.commit()

    @staticmethod
    def stamp(p: Path) -> tuple[str, int, int]:
        st = p.stat()
        return _norm(p), st.st_size, st.st_mtime_ns

    def get(self, p: Path) -> PhotoRecord | None:
        try:
            key, size, mtime = self.stamp(p)
        except OSError:
            return None
        row = self.db.execute(
            "SELECT sha1,width,height,mean_rgb,mean_lab,grid_lab,thumb,warning FROM photos "
            "WHERE key=? AND size=? AND mtime=? AND ver=?", (key, size, mtime, CACHE_VERSION)).fetchone()
        if not row:
            return None
        sha1, w, h, mr, ml, gl, thumb, warning = row
        f = lambda b: np.frombuffer(b, dtype=np.float32).astype(float).tolist()  # noqa: E731
        return PhotoRecord(str(p), sha1, w, h, f(mr), f(ml), f(gl), thumb, warning or "")

    def put_many(self, items: list[tuple[Path, PhotoRecord]]) -> None:
        rows = []
        for p, r in items:
            try:
                key, size, mtime = self.stamp(p)
            except OSError:
                continue
            b = lambda v: np.asarray(v, dtype=np.float32).tobytes()  # noqa: E731
            rows.append((key, size, mtime, CACHE_VERSION, r.sha1, r.width, r.height,
                         b(r.mean_rgb), b(r.mean_lab), b(r.grid_lab), r.thumb_jpeg, r.warning))
        if rows:
            self.db.executemany("INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.db.commit()

    def close(self) -> None:
        self.db.close()


# --------------------------------------------------------------------------
# 1枚の解析
# --------------------------------------------------------------------------
def analyze_file(p: Path) -> PhotoRecord:
    """写真1枚を検査・解析する。失敗したら ImageLoadError。"""
    try:
        size = p.stat().st_size
    except OSError:
        raise ImageLoadError(f"ファイルを開けません：{p.name}", "ファイルが移動・削除されていないか確認してください。")
    if size > MAX_FILE_BYTES:
        raise ImageLoadError(f"ファイルが大きすぎます（{size / 1024 / 1024:.0f}MB）：{p.name}", "写真を縮小してから使ってください。")
    try:
        data = p.read_bytes()
    except OSError as e:
        raise ImageLoadError(f"ファイルを読み込めません：{p.name}", f"アクセス権を確認してください。（{e}）")
    sha1 = hashlib.sha1(data).hexdigest()
    li = image_io.load_image(p, max_side=ANALYSIS_SIDE, data=data)
    del data
    img = li.image
    small = img.resize((FEATURE_SIDE, FEATURE_SIDE), Image.Resampling.BOX)
    mean_rgb, mean_lab, grid_lab = image_features(np.asarray(small))
    thumb = img.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, "JPEG", quality=82)
    w, h = li.original_size
    return PhotoRecord(str(p), sha1, w, h, mean_rgb, mean_lab, grid_lab, buf.getvalue(), li.warning)


# --------------------------------------------------------------------------
# まとめて解析
# --------------------------------------------------------------------------
def analyze_sources(
    sources: Iterable[str],
    recursive: bool = False,
    known_paths: set[str] | None = None,
    known_sha1: set[str] | None = None,
    capacity: int = config.MAX_TILE_PHOTOS,
    progress: Callable[[int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    cache: AnalysisCache | None = None,
    workers: int | None = None,
) -> BatchResult:
    """フォルダー／ファイルを解析して BatchResult を返す。

    known_paths / known_sha1 は、すでにコレクションにある写真（重複判定用）。
    capacity は、あと何枚追加できるか（上限2,000枚 − 登録済みの枚数）。
    """
    t0 = time.time()
    res = BatchResult()
    known_paths = set(known_paths or ())
    sha_seen = set(known_sha1 or ())
    candidates, res.ignored = expand_sources(sources, recursive)
    before = len(candidates)
    candidates = [c for c in candidates if _norm(c) not in known_paths]
    res.already = before - len(candidates)
    total = len(candidates)
    done = 0
    report = progress or (lambda d, t: None)
    cancelled = is_cancelled or (lambda: False)
    own_cache = cache is None
    try:
        cache = cache or AnalysisCache()
    except sqlite3.Error:
        cache = None  # 保存庫が使えなくても解析自体は続ける

    def accept(rec: PhotoRecord) -> bool:
        if rec.sha1 in sha_seen:
            res.duplicates.append(rec.path)
            return False
        if len(res.records) >= capacity:
            res.over_limit += 1
            return False
        sha_seen.add(rec.sha1)
        res.records.append(rec)
        return True

    # ① 前回の解析結果が使える写真
    todo: list[Path] = []
    for p in candidates:
        if cancelled():
            res.cancelled = True
            break
        rec = cache.get(p) if cache else None
        if rec:
            res.from_cache += 1
            accept(rec)
            done += 1
            if done % 50 == 0:
                report(done, total)
        else:
            todo.append(p)
    report(done, total)

    # ② 新しい写真を並列で解析
    if todo and not res.cancelled:
        n = workers or max(2, min(8, (os.cpu_count() or 4)))
        to_store: list[tuple[Path, PhotoRecord]] = []
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = {ex.submit(analyze_file, p): p for p in todo}
            try:
                for fut in as_completed(futures):
                    p = futures[fut]
                    try:
                        rec = fut.result()
                        to_store.append((p, rec))
                        accept(rec)
                    except ImageLoadError as e:
                        res.failed.append((str(p), e.cause))
                    except MemoryError:
                        res.failed.append((str(p), "メモリが足りず読み込めませんでした"))
                    except Exception as e:  # 想定外のエラーでも残りの写真は続ける
                        res.failed.append((str(p), f"読み込めませんでした（{e}）"))
                    done += 1
                    report(done, total)
                    if len(to_store) >= 100 and cache:
                        cache.put_many(to_store)
                        to_store.clear()
                    if cancelled():
                        res.cancelled = True
                        for f in futures:
                            f.cancel()
                        break
            finally:
                if cache and to_store:
                    cache.put_many(to_store)

    if cache and own_cache:
        cache.close()
    # 並列処理で順番がばらばらになるので、ファイル名順に並べ直す
    res.records.sort(key=lambda r: r.path.lower())
    res.seconds = time.time() - t0
    return res


# --------------------------------------------------------------------------
# コレクション本体
# --------------------------------------------------------------------------
class PhotoCollection:
    """登録済みの写真の一覧。画面側（メインスレッド）だけが変更する。"""

    def __init__(self):
        self.records: list[PhotoRecord] = []
        self.sources: list[str] = []      # 追加に使ったフォルダー／ファイル（次回の自動復元用）
        self.failed: list[tuple[str, str]] = []
        self.duplicates: list[str] = []
        self.excluded: set[str] = set()   # 利用者が「外した」写真（フォルダーを読み直しても戻さない）
        self.excluded_sha1: set[str] = set()  # 外した写真の指紋（名前違いの同じ写真も戻さない）

    def __len__(self) -> int:
        return len(self.records)

    @property
    def capacity(self) -> int:
        return max(0, config.MAX_TILE_PHOTOS - len(self.records))

    def known_paths(self) -> set[str]:
        return {_norm(r.path) for r in self.records}

    def skip_paths(self) -> set[str]:
        """読み込みを省く写真：登録済み＋利用者が外した写真。"""
        return self.known_paths() | self.excluded

    def remove_indices(self, indices: Iterable[int]) -> list[str]:
        """指定した番号の写真をコレクションから外す（元のファイルは削除しない）。"""
        idx = sorted({i for i in indices if 0 <= i < len(self.records)}, reverse=True)
        removed = []
        for i in idx:
            rec = self.records.pop(i)
            self.excluded.add(_norm(rec.path))
            self.excluded_sha1.add(rec.sha1)
            removed.append(rec.path)
        return removed[::-1]

    def unexclude(self, paths: Iterable[str]) -> None:
        """「外した」写真を、利用者がもう一度追加したときは戻せるようにする。"""
        for p in paths:
            self.excluded.discard(_norm(p))

    def known_sha1(self, include_excluded: bool = True) -> set[str]:
        shas = {r.sha1 for r in self.records}
        return shas | self.excluded_sha1 if include_excluded else shas

    def merge(self, result: BatchResult, sources: Iterable[str]) -> None:
        self.records.extend(result.records)
        self.records.sort(key=lambda r: r.path.lower())
        self.failed.extend(result.failed)
        self.duplicates.extend(result.duplicates)
        for s in sources:
            if s not in self.sources:
                self.sources.append(s)

    def clear(self) -> None:
        self.records.clear()
        self.sources.clear()
        self.failed.clear()
        self.duplicates.clear()
        self.excluded.clear()
        self.excluded_sha1.clear()

    def lab_matrix(self) -> np.ndarray:
        """モザイク生成用：全写真の平均Lab（形 [枚数, 3]）。"""
        return np.array([r.mean_lab for r in self.records], dtype=np.float32).reshape(-1, 3)
