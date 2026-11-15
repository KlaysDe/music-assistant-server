from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_channel


class SourceTransferTests(unittest.TestCase):
    def test_source_clone_can_serve_historical_blobs_to_the_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / "upstream"
            upstream.mkdir()
            run = sync_channel.run
            run(["git", "init", "--quiet"], cwd=upstream)
            for key, value in (("user.name", "Test"), ("user.email", "test@example.test"),
                               ("uploadpack.allowFilter", "true")):
                run(["git", "config", key, value], cwd=upstream)
            for contents in ("historical", "current"):
                (upstream / "source.txt").write_text(contents, encoding="utf-8")
                run(["git", "add", "source.txt"], cwd=upstream)
                run(["git", "commit", "--quiet", "--no-gpg-sign", "-m", contents], cwd=upstream)
            run(["git", "tag", "nightly"], cwd=upstream)
            candidate = root / "candidate"
            with patch.dict(os.environ, {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{upstream.as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/example/server.git",
            }):
                commit = sync_channel.git_init_source(candidate, "example/server", "nightly")

            publisher = root / "publisher"
            publisher.mkdir()
            run(["git", "init", "--quiet"], cwd=publisher)
            run(["git", "fetch", str(candidate), f"{commit}:refs/ha-store-staging/server"], cwd=publisher)
            self.assertEqual(run(["git", "show", f"{commit}^:source.txt"], cwd=publisher), "historical")


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
