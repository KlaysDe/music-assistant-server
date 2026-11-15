"""Exercise migration with Supervisor-shaped archives and synthetic app data."""

import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from tools.migrate_backup import DEV_APP, OPTIONS, BackupError, migrate_backup

TARGET = "1234abcd_music_assistant"
SETTINGS = b'{"server_id":"source-id","providers":{},"encryption_key":"source-key"}'
SQLITE = b"SQLite format 3\x00" + bytes(100)
SESSION = "data/spotify/spotify--abc/soloist-data/settings/Users/account-user/session"
SOURCE_FILES = {
    "data/settings.json": SETTINGS,
    "data/auth.db": SQLITE + b"source-users",
    "data/library.db": SQLITE + b"source-library",
    "data/library.db-wal": b"source-wal",
    "data/playlists/playlist.m3u": b"source-playlist",
    "config/source.json": b'{"config":true}',
    SESSION: b"source-credential",
    OPTIONS: b'{"server_repo":"dev-overlay"}',
    "apparmor.txt": b"source-profile",
    "image.tar": b"source-image",
}
TARGET_FILES = {
    OPTIONS: b'{"log_level":"info"}',
    "apparmor.txt": b"target-profile",
    "image.tar": b"target-image",
}


def _add(archive, name, data):
    info = tarfile.TarInfo(f"./{name}")
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def _make_backup(
    path,
    app,
    files,
    *,
    compressed=False,
    state="stopped",
    repository=None,
    supervisor="2026.11.0",
    protected=False,
    extra_member=None,
):
    repo, slug = app.split("_", 1)
    version = "2.11.0.dev20261115" if app == DEV_APP else "2.11.0.20261115"
    app_meta = {
        "state": state,
        "version": version,
        "system": {"slug": slug, "repository": repository or repo},
        "user": {
            "version": version,
            "options": json.loads(files.get(OPTIONS, b"{}")),
            "ingress_token": f"{slug}-ingress-token",
            "auto_update": app != DEV_APP,
        },
    }
    manifest = {
        "version": 2,
        "type": "partial",
        "supervisor_version": supervisor,
        "addons": [{"slug": app, "name": slug, "version": version}],
        "compressed": compressed,
        "protected": protected,
        "homeassistant": None,
        "folders": [],
        "repositories": ["https://example.test/source"],
    }
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w:gz" if compressed else "w:") as archive:
        for directory in (".", "data", "config"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        _add(archive, "addon.json", json.dumps(app_meta).encode())
        for name, data in files.items():
            _add(archive, name, data)
        if extra_member:
            archive.addfile(extra_member)
    with tarfile.open(path, "w:") as outer:
        _add(outer, "backup.json", json.dumps(manifest).encode())
        suffix = ".tar.gz" if compressed else ".tar"
        _add(outer, f"{app}{suffix}", inner.getvalue())
    return app_meta, manifest


def _files(archive):
    files = {}
    for member in archive:
        if member.isfile():
            with archive.extractfile(member) as stream:
                files[member.name.removeprefix("./")] = stream.read()
    return files


class MigrateBackupTest(unittest.TestCase):
    def setUp(self):
        working = tempfile.TemporaryDirectory()
        self.addCleanup(working.cleanup)
        self.base = Path(working.name)
        self.source = self.base / "dev.tar"
        self.target = self.base / "target.tar"
        self.output = self.base / "restore.tar"

    def test_preserves_all_source_data_and_keeps_target_identity_and_options(self):
        for source_compressed, target_compressed in ((True, False), (False, True)):
            with self.subTest(target_compressed=target_compressed):
                _make_backup(
                    self.source, DEV_APP, SOURCE_FILES,
                    compressed=source_compressed, state="error",
                )
                metadata, template = _make_backup(
                    self.target, TARGET, TARGET_FILES, compressed=target_compressed,
                )
                slug = migrate_backup(self.source, self.target, self.output, TARGET)
                with tarfile.open(self.output, "r:") as outer:
                    files = _files(outer)
                manifest = json.loads(files.pop("backup.json"))
                self.assertEqual(manifest["addons"], template["addons"])
                self.assertEqual(manifest["slug"], slug)
                self.assertEqual(manifest["repositories"], [])
                self.assertIs(manifest["protected"], False)
                self.assertIs(manifest["compressed"], target_compressed)
                self.assertEqual(manifest["folders"], [])
                self.assertIsNone(manifest["homeassistant"])
                suffix = ".tar.gz" if target_compressed else ".tar"
                self.assertEqual(list(files), [TARGET + suffix])
                with tarfile.open(fileobj=io.BytesIO(files[TARGET + suffix])) as inner:
                    files = _files(inner)
                self.assertEqual(json.loads(files.pop("addon.json")), metadata)
                self.assertEqual(files, SOURCE_FILES | TARGET_FILES)
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
                self.output.unlink()

    def test_cli_reuses_exact_installed_image_and_keeps_absent_target_options(self):
        _make_backup(self.source, DEV_APP, SOURCE_FILES)
        _make_backup(self.target, TARGET, {"apparmor.txt": b"target-profile"})
        script = Path(__file__).resolve().parents[1] / "tools" / "migrate_backup.py"
        result = subprocess.run(
            [sys.executable, str(script), str(self.source), str(self.target), str(self.output),
             "--target-app", TARGET, "--reuse-installed-image"],
            check=True, capture_output=True, text=True,
        )
        self.assertIn(f"Prepared restore for {TARGET}", result.stdout)
        self.assertNotIn("source-credential", result.stdout + result.stderr)
        with tarfile.open(self.output, "r:") as outer:
            with tarfile.open(fileobj=outer.extractfile(f"./{TARGET}.tar")) as inner:
                files = _files(inner)
        self.assertNotIn("image.tar", files)
        self.assertNotIn(OPTIONS, files)
        self.assertEqual(files[SESSION], SOURCE_FILES[SESSION])
        self.assertEqual(files["apparmor.txt"], b"target-profile")

    def test_omits_source_pnpm_build_cache_with_a_symlink(self):
        project = tarfile.TarInfo("./data/.pnpm-store/v11/projects/frontend")
        project.type = tarfile.SYMTYPE
        project.linkname = "../../../frontend-build"
        _make_backup(
            self.source, DEV_APP,
            SOURCE_FILES | {"data/.pnpm-store/v11/cache": b"build cache"},
            extra_member=project,
        )
        _make_backup(self.target, TARGET, TARGET_FILES)
        migrate_backup(self.source, self.target, self.output, TARGET)
        with tarfile.open(self.output, "r:") as outer:
            with tarfile.open(fileobj=outer.extractfile(f"./{TARGET}.tar")) as inner:
                self.assertFalse(any(".pnpm-store" in item for item in inner.getnames()))
                self.assertEqual(_files(inner)[SESSION], SOURCE_FILES[SESSION])

    def test_rejects_live_or_nonempty_target(self):
        _make_backup(self.source, DEV_APP, SOURCE_FILES)
        _make_backup(self.target, TARGET, TARGET_FILES, state="started")
        with self.assertRaisesRegex(BackupError, "stopped app"):
            migrate_backup(self.source, self.target, self.output, TARGET)
        for name in ("data/settings.json", "config/user-config.json"):
            with self.subTest(name=name):
                _make_backup(self.target, TARGET, TARGET_FILES | {name: b"target data"})
                with self.assertRaisesRegex(BackupError, "fresh target"):
                    migrate_backup(self.source, self.target, self.output, TARGET)
        self.assertFalse(self.output.exists())

    def test_rejects_wrong_app_metadata_and_supervisor_versions(self):
        _make_backup(self.source, DEV_APP, SOURCE_FILES)
        _make_backup(self.target, TARGET, TARGET_FILES, repository="feedfeed")
        with self.assertRaisesRegex(BackupError, "stopped app"):
            migrate_backup(self.source, self.target, self.output, TARGET)
        _make_backup(self.target, TARGET, TARGET_FILES, supervisor="2026.10.0")
        with self.assertRaisesRegex(BackupError, "same Supervisor"):
            migrate_backup(self.source, self.target, self.output, TARGET)
        with self.assertRaisesRegex(BackupError, "full Supervisor app ID"):
            migrate_backup(self.source, self.target, self.output, DEV_APP)
        self.assertFalse(self.output.exists())

    def test_rejects_protected_source_and_invalid_auth_db(self):
        _make_backup(self.target, TARGET, TARGET_FILES)
        _make_backup(self.source, DEV_APP, SOURCE_FILES, protected=True)
        with self.assertRaisesRegex(BackupError, "unencrypted partial backup"):
            migrate_backup(self.source, self.target, self.output, TARGET)
        _make_backup(self.source, DEV_APP, SOURCE_FILES | {"data/auth.db": b"broken"})
        with self.assertRaisesRegex(BackupError, "auth.db database is missing or invalid"):
            migrate_backup(self.source, self.target, self.output, TARGET)
        self.assertFalse(self.output.exists())

    def test_rejects_traversal_duplicate_and_symlink_without_leaking_paths(self):
        _make_backup(self.target, TARGET, TARGET_FILES)
        traversal = tarfile.TarInfo("data/../../private-path")
        duplicate = tarfile.TarInfo("./data//settings.json")
        symlink = tarfile.TarInfo("data/link")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "/private-path"
        for member in (traversal, duplicate, symlink):
            with self.subTest(member=member.name):
                _make_backup(self.source, DEV_APP, SOURCE_FILES, extra_member=member)
                with self.assertRaisesRegex(BackupError, "unsupported or duplicate entry"):
                    migrate_backup(self.source, self.target, self.output, TARGET)
        self.assertFalse(self.output.exists())

    def test_requires_target_image_without_explicit_reuse_and_protects_existing_output(self):
        _make_backup(self.source, DEV_APP, SOURCE_FILES)
        _make_backup(self.target, TARGET, {OPTIONS: TARGET_FILES[OPTIONS]})
        with self.assertRaisesRegex(BackupError, "lacks its built image"):
            migrate_backup(self.source, self.target, self.output, TARGET)
        self.output.write_bytes(b"previous backup")
        with self.assertRaisesRegex(BackupError, "Choose a new"):
            migrate_backup(self.source, self.target, self.output, TARGET)
        self.assertEqual(self.output.read_bytes(), b"previous backup")


if __name__ == "__main__":
    unittest.main()
