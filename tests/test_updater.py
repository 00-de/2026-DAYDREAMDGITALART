"""自動更新のテスト（ネットには接続せず、GitHub の応答を模擬して確認する）。"""

import hashlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, updater  # noqa: E402

SETUP = config.SETUP_ASSET_NAME
PAYLOAD = b"MZ fake installer bytes" * 1000


def release(tag="v0.2.9", payload=PAYLOAD, draft=False, prerelease=False, with_sha=True):
    assets = [{"name": SETUP, "size": len(payload),
               "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{SETUP}"}]
    if with_sha:
        assets.append({"name": SETUP + ".sha256", "size": 90,
                       "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{SETUP}.sha256"})
    return {"tag_name": tag, "body": "・文字モザイクを改善", "draft": draft,
            "prerelease": prerelease, "assets": assets}


class FakeResp(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class VersionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v0.2.15"), (0, 2, 15))
        self.assertEqual(updater.parse_version("0.2.0-dev"), (0, 2, 0))
        self.assertEqual(updater.parse_version("1.0"), (1, 0, 0))
        self.assertEqual(updater.parse_version("abc"), (0,))

    def test_compare(self):
        self.assertTrue(updater.is_newer("0.2.10", "0.2.9"))      # 文字列比較だと逆になる例
        self.assertTrue(updater.is_newer("v0.3.1", "0.2.99"))
        self.assertFalse(updater.is_newer("0.2.9", "0.2.9"))
        self.assertFalse(updater.is_newer("0.2.8", "0.2.9"))


class ReleaseParseTest(unittest.TestCase):
    def test_valid(self):
        info = updater.parse_release(release())
        self.assertEqual(info.version, "0.2.9")
        self.assertTrue(info.setup_url.endswith(SETUP))

    def test_ignores_draft_prerelease_and_missing_hash(self):
        self.assertIsNone(updater.parse_release(release(draft=True)))
        self.assertIsNone(updater.parse_release(release(prerelease=True)))
        self.assertIsNone(updater.parse_release(release(with_sha=False)))


class CheckTest(unittest.TestCase):
    def _patch(self, data):
        import json
        return mock.patch.object(updater, "_request", return_value=FakeResp(json.dumps(data).encode()))

    def test_newer_found(self):
        with self._patch(release("v0.2.9")):
            self.assertIsNotNone(updater.check_for_update("o/r", "0.2.5"))

    def test_same_or_older(self):
        with self._patch(release("v0.2.5")):
            self.assertIsNone(updater.check_for_update("o/r", "0.2.5"))

    def test_offline_is_silent(self):
        import urllib.error
        with mock.patch.object(updater, "_request", side_effect=urllib.error.URLError("offline")):
            self.assertIsNone(updater.check_for_update("o/r", "0.2.5"))

    def test_no_repo_no_network(self):
        with mock.patch.object(updater, "_request") as m:
            self.assertIsNone(updater.check_for_update("", "0.2.5"))
            m.assert_not_called()

    def test_blocks_unknown_hosts(self):
        with self.assertRaises(updater.UpdateError):
            updater._request("https://evil.example.com/setup.exe", 1)


class DownloadTest(unittest.TestCase):
    def _fake(self, payload, sha_text):
        def fake_request(url, timeout):
            return FakeResp(sha_text.encode() if url.endswith(".sha256") else payload)
        return mock.patch.object(updater, "_request", side_effect=fake_request)

    def test_download_and_verify(self):
        info = updater.parse_release(release())
        sha = hashlib.sha256(PAYLOAD).hexdigest()
        seen = []
        with self._fake(PAYLOAD, f"{sha}  {SETUP}\n"):
            path = updater.download_update(info, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertEqual(seen[-1], (len(PAYLOAD), len(PAYLOAD)))
        path.unlink()

    def test_tampered_file_rejected_and_deleted(self):
        info = updater.parse_release(release())
        sha = hashlib.sha256(PAYLOAD).hexdigest()
        tampered = PAYLOAD[:-1] + b"X"
        with self._fake(tampered, sha):
            with self.assertRaises(updater.UpdateError) as cm:
                updater.download_update(info)
        self.assertIn("改ざん", str(cm.exception))

    def test_cancel(self):
        info = updater.parse_release(release())
        sha = hashlib.sha256(PAYLOAD).hexdigest()
        with self._fake(PAYLOAD, sha):
            with self.assertRaises(updater.UpdateError):
                updater.download_update(info, is_cancelled=lambda: True)

    def test_dev_build_has_updates_disabled(self):
        self.assertFalse(updater.updates_supported())


if __name__ == "__main__":
    unittest.main(verbosity=2)
