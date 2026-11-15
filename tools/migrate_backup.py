#!/usr/bin/env python3
"""Prepare an app-only Home Assistant restore from Music Assistant DEV data.

Take unencrypted partial backups of both stopped apps on the same Supervisor.
The target must be the newly installed custom app, before it has app data. Run
this tool with Python 3.10+ in a private local directory. On Unix the filesystem
must support hard links. The output is unencrypted and contains account data.
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

DEV_APP = "d5369777_music_assistant_dev"
OPTIONS = "data/options.json"
PNPM_STORE = PurePosixPath("data/.pnpm-store")
APP_METADATA = {".", "addon.json", "apparmor.txt", "image.tar"}
APP_ID = re.compile(r"([0-9a-f]{8}|local)_([a-z0-9][a-z0-9_-]*)")
JSON_LIMIT = 32 * 1024 * 1024


class BackupError(Exception):
    """A validation failure safe to print without exposing backup contents."""


@dataclass
class AppBackup:
    """The metadata and indexed app archive in a Supervisor backup."""

    manifest: dict[str, Any]
    archive: tarfile.TarFile
    members: dict[str, tarfile.TarInfo]

    @property
    def compressed(self) -> bool:
        return self.manifest["compressed"]


def _members(
    archive: tarfile.TarFile, *, exclude: PurePosixPath | None = None
) -> dict[str, tarfile.TarInfo]:
    members = {}
    for member in archive:
        path = PurePosixPath(member.name)
        name = path.as_posix()
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in member.name
            or any(":" in part for part in path.parts)
        ):
            raise BackupError("An archive contains an unsafe path.")
        if exclude is not None and path.is_relative_to(exclude):
            continue
        if (
            not (member.isfile() or member.isdir())
            or member.issparse()
            or (name == "." and not member.isdir())
            or name in members
        ):
            raise BackupError("An archive contains an unsupported or duplicate entry.")
        members[name] = member
    for name in members:
        for parent in PurePosixPath(name).parents:
            if (entry := members.get(parent.as_posix())) is not None and not entry.isdir():
                raise BackupError("An archive contains a file where a directory is required.")
    return members


def _read_json(
    archive: tarfile.TarFile, member: tarfile.TarInfo | None, label: str
) -> dict[str, Any]:
    if member is None or not member.isfile() or not 0 < member.size <= JSON_LIMIT:
        raise BackupError(f"{label} is missing or invalid.")
    with archive.extractfile(member) as stream:
        try:
            value = json.load(stream)
        except ValueError:
            raise BackupError(f"{label} is invalid.") from None
    if not isinstance(value, dict):
        raise BackupError(f"{label} is invalid.")
    return value


def _open_backup(
    stack: ExitStack, path: Path, app_id: str, *, allow_error_state: bool = False
) -> AppBackup:
    outer = stack.enter_context(tarfile.open(path, "r:"))
    outer_members = _members(outer)
    manifest = _read_json(outer, outer_members.get("backup.json"), "Backup manifest")
    apps = manifest.get("addons")
    if (
        manifest.get("version") != 2
        or manifest.get("type") != "partial"
        or manifest.get("protected", False) is not False
        or not isinstance(manifest.get("compressed"), bool)
        or not isinstance(manifest.get("supervisor_version"), str)
        or manifest.get("folders", []) != []
        or manifest.get("homeassistant") is not None
        or not isinstance(apps, list)
        or len(apps) != 1
        or not isinstance(apps[0], dict)
        or apps[0].get("slug") != app_id
        or not isinstance(apps[0].get("name"), str)
        or not isinstance(apps[0].get("version"), str)
    ):
        raise BackupError("Expected an unencrypted partial backup containing only the specified app.")

    suffix = ".tar.gz" if manifest["compressed"] else ".tar"
    app_member = outer_members.get(app_id + suffix)
    if app_member is None or not app_member.isfile():
        raise BackupError("The manifest's app archive is missing.")
    inner_stream = stack.enter_context(outer.extractfile(app_member))
    mode = "r:gz" if manifest["compressed"] else "r:"
    inner = stack.enter_context(tarfile.open(fileobj=inner_stream, mode=mode))
    # The DEV installer recreates its package store, including its build-directory symlink.
    members = _members(inner, exclude=PNPM_STORE if app_id == DEV_APP else None)
    addon = _read_json(inner, members.get("addon.json"), "App metadata")
    system = addon.get("system")
    user = addon.get("user")
    repository, _, slug = app_id.partition("_")
    # Supervisor may leave the source in error after stopping a failed container.
    allowed_states = {"stopped", "error"} if allow_error_state else {"stopped"}
    if (
        not isinstance(system, dict)
        or system.get("slug") != slug
        or system.get("repository") != repository
        or not isinstance(user, dict)
        or not isinstance(user.get("options"), dict)
        or addon.get("version") != apps[0]["version"]
        or user.get("version") != apps[0]["version"]
        or addon.get("state") not in allowed_states
    ):
        raise BackupError("App metadata does not describe the expected stopped app.")
    for name, member in members.items():
        root = name.split("/", 1)[0]
        if (name not in APP_METADATA and root not in {"data", "config"}) or (
            name in {"data", "config"} and not member.isdir()
        ):
            raise BackupError("The app archive contains an unexpected entry.")
    if OPTIONS in members:
        _read_json(inner, members[OPTIONS], "App options")
    for name in ("apparmor.txt", "image.tar"):
        if (entry := members.get(name)) is not None and (not entry.isfile() or not entry.size):
            raise BackupError("The app archive contains an invalid image or AppArmor profile.")
    return AppBackup(manifest, inner, members)


def _validate_source(source: AppBackup) -> None:
    settings = _read_json(source.archive, source.members.get("data/settings.json"), "DEV settings")
    server_id = settings.get("server_id")
    if not isinstance(server_id, str) or not server_id or not isinstance(settings.get("providers"), dict):
        raise BackupError("DEV settings do not contain a server and provider configuration.")
    for db in ("library.db", "auth.db"):
        member = source.members.get(f"data/{db}")
        if member is not None and member.isfile() and member.size >= 100:
            with source.archive.extractfile(member) as stream:
                if stream.read(16) == b"SQLite format 3\x00":
                    continue
        raise BackupError(f"The DEV {db} database is missing or invalid.")


def _validate_target(target: AppBackup, reuse_installed_image: bool) -> None:
    for name, member in target.members.items():
        if name == OPTIONS or name in APP_METADATA:
            continue
        if not member.isdir():
            raise BackupError("Use a fresh target app without existing application data.")
    if not reuse_installed_image and "image.tar" not in target.members:
        raise BackupError(
            "Target backup lacks its built image. "
            "Keep the exact target image installed and use --reuse-installed-image."
        )


def _copy_member(
    destination: tarfile.TarFile, backup: AppBackup, member: tarfile.TarInfo
) -> None:
    if member.isdir():
        destination.addfile(copy.copy(member))
    else:
        with backup.archive.extractfile(member) as stream:
            destination.addfile(copy.copy(member), stream)


def _write_inner(
    path: Path, source: AppBackup, target: AppBackup, reuse_installed_image: bool
) -> None:
    mode = "w:gz" if target.compressed else "w:"
    with tarfile.open(path, mode) as result:
        for name, member in target.members.items():
            if name in APP_METADATA and not (name == "image.tar" and reuse_installed_image):
                _copy_member(result, target, member)
        for name, member in source.members.items():
            if name not in APP_METADATA and name != OPTIONS:
                _copy_member(result, source, member)
        if OPTIONS in target.members:
            _copy_member(result, target, target.members[OPTIONS])


def _write_outer(destination: Path, inner: Path, target: AppBackup, target_app: str) -> str:
    slug = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    manifest = {
        "version": 2,
        "slug": slug,
        "name": "Music Assistant DEV data migration",
        "date": now.isoformat(),
        "type": "partial",
        "supervisor_version": target.manifest["supervisor_version"],
        "compressed": target.compressed,
        "protected": False,
        "homeassistant": None,
        "folders": [],
        "addons": target.manifest["addons"],
        "repositories": [],
        "extra": {},
    }
    encoded = json.dumps(manifest).encode("utf-8")
    info = tarfile.TarInfo("./backup.json")
    info.size = len(encoded)
    info.mode = 0o600
    info.mtime = int(now.timestamp())
    suffix = ".tar.gz" if target.compressed else ".tar"
    with tempfile.NamedTemporaryFile(dir=inner.parent, delete=False) as temp:
        with tarfile.open(fileobj=temp, mode="w:") as result:
            result.add(inner, arcname=f"./{target_app}{suffix}")
            result.addfile(info, io.BytesIO(encoded))
        temp.flush()
        os.fsync(temp.fileno())
    # Publish a complete file without overwriting anything created concurrently.
    if os.name == "nt":
        os.rename(temp.name, destination)
    else:
        os.link(temp.name, destination)
    return slug


def migrate_backup(
    source_path: Path,
    target_path: Path,
    output: Path,
    target_app: str,
    *,
    reuse_installed_image: bool = False,
) -> str:
    """Build a new restore tar and return its Supervisor backup slug."""
    if not APP_ID.fullmatch(target_app) or target_app == DEV_APP:
        raise BackupError("Specify the custom add-on's full Supervisor app ID as the target.")
    if output.suffix != ".tar" or output.exists():
        raise BackupError("Choose a new .tar output file.")
    with ExitStack() as stack:
        source = _open_backup(stack, source_path, DEV_APP, allow_error_state=True)
        target = _open_backup(stack, target_path, target_app)
        if source.manifest["supervisor_version"] != target.manifest["supervisor_version"]:
            raise BackupError("Take both backups on the same Supervisor version.")
        _validate_source(source)
        _validate_target(target, reuse_installed_image)
        work = Path(stack.enter_context(
            tempfile.TemporaryDirectory(prefix=".ma-restore-", dir=output.parent)
        ))
        inner = work / "app.tar"
        _write_inner(inner, source, target, reuse_installed_image)
        return _write_outer(output, inner, target, target_app)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dev_backup", type=Path)
    parser.add_argument("target_template", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--target-app", required=True, help="Full Supervisor app ID of the custom app.")
    parser.add_argument(
        "--reuse-installed-image",
        action="store_true",
        help="Omit the target image export when its exact version remains installed on this host.",
    )
    args = parser.parse_args()
    try:
        slug = migrate_backup(
            args.dev_backup,
            args.target_template,
            args.output,
            args.target_app,
            reuse_installed_image=args.reuse_installed_image,
        )
    except BackupError as error:
        print(error, file=sys.stderr)
        return 1
    except (OSError, tarfile.TarError, EOFError, ValueError):
        print("Could not read an archive or safely write the restore backup.", file=sys.stderr)
        return 1
    print(f"Prepared restore for {args.target_app} with backup slug {slug}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
