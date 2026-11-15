from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_channel


class NightlySelectionTests(unittest.TestCase):
    def test_selects_latest_published_nightly_and_ignores_other_releases(self) -> None:
        releases = [
            {"tag_name": "2.11.0", "published_at": "2026-11-15T07:00:00Z", "prerelease": False},
            {"tag_name": "2.11.0.dev2026111403", "published_at": "2026-11-14T03:00:00Z", "prerelease": True},
            {"tag_name": "2.11.0.dev2026111503", "published_at": "2026-11-15T03:00:00Z", "prerelease": True},
            {"tag_name": "2.11.0.dev2026111603", "published_at": "2026-11-16T03:00:00Z", "prerelease": True, "draft": True},
        ]
        self.assertEqual(sync_channel.newest_nightly(releases)["tag_name"], "2.11.0.dev2026111503")

    def test_requires_an_official_nightly(self) -> None:
        with self.assertRaises(sync_channel.SyncError):
            sync_channel.newest_nightly([{"tag_name": "2.11.0", "prerelease": False}])


class FrontendDependencyTests(unittest.TestCase):
    def test_resolves_frontend_version_from_the_selected_server_manifest(self) -> None:
        pyproject = '''
[project]
dependencies = [
  "music-assistant-frontend @ https://github.com/music-assistant/frontend/releases/download/2.17.369/music_assistant_frontend-2.17.369-py3-none-any.whl#sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
]
'''
        self.assertEqual(
            sync_channel.parse_frontend_dependency(pyproject),
            ("2.17.369", "a" * 64),
        )

    def test_rejects_a_frontend_url_and_wheel_version_mismatch(self) -> None:
        pyproject = '''
[project]
dependencies = ["music-assistant-frontend @ https://github.com/music-assistant/frontend/releases/download/2.17.369/music_assistant_frontend-2.17.368-py3-none-any.whl#sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
'''
        with self.assertRaises(sync_channel.SyncError):
            sync_channel.parse_frontend_dependency(pyproject)


class ChannelPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "music_assistant").mkdir()
        (self.root / "music_assistant" / "config.yaml").write_text(
            'name: Music Assistant\nversion: "0.1.0"\n', encoding="utf-8"
        )
        (self.root / "repository.yaml").write_text("name: Store repository\n", encoding="utf-8")
        (self.root / "music_assistant" / "Dockerfile").write_text(
            "FROM ghcr.io/music-assistant/server:dev@sha256:" + "a" * 64 + "\n"
            "ENV SERVER_REPO=KlaysDe/music-assistant-server@" + "b" * 40 + "\n"
            "ENV FRONTEND_REPO=KlaysDe/music-assistant-server@" + "c" * 40 + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pins_bump_only_the_channel_version_and_three_declared_values(self) -> None:
        updated = sync_channel.update_channel_files(self.root, "sha256:" + "d" * 64, "e" * 40, "f" * 40)
        self.assertTrue(updated)
        config = (self.root / "music_assistant" / "config.yaml").read_text(encoding="utf-8")
        dockerfile = (self.root / "music_assistant" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('version: "0.1.1"', config)
        self.assertIn("FROM ghcr.io/music-assistant/server:dev@sha256:" + "d" * 64, dockerfile)
        self.assertIn("ENV SERVER_REPO=KlaysDe/music-assistant-server@" + "e" * 40, dockerfile)
        self.assertIn("ENV FRONTEND_REPO=KlaysDe/music-assistant-server@" + "f" * 40, dockerfile)
        self.assertEqual((self.root / "repository.yaml").read_text(encoding="utf-8"), "name: Store repository\n")

    def test_repeating_identical_pins_is_a_noop(self) -> None:
        digest = "sha256:" + "a" * 64
        self.assertFalse(sync_channel.update_channel_files(self.root, digest, "b" * 40, "c" * 40))
        config = (self.root / "music_assistant" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn('version: "0.1.0"', config)

    def test_duplicate_dockerfile_pins_fail_closed(self) -> None:
        dockerfile = self.root / "music_assistant" / "Dockerfile"
        dockerfile.write_text(
            dockerfile.read_text(encoding="utf-8")
            + "ENV SERVER_REPO=KlaysDe/music-assistant-server@" + "b" * 40 + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(sync_channel.SyncError):
            sync_channel.update_channel_files(self.root, "sha256:" + "d" * 64, "e" * 40, "f" * 40)


if __name__ == "__main__":
    unittest.main()
