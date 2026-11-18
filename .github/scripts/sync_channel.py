#!/usr/bin/env python3
"""Qualify and publish a pinned HA Store build from the latest upstream nightly."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SERVER_REPOSITORY = "KlaysDe/music-assistant-server"
UPSTREAM_SERVER = "music-assistant/server"
UPSTREAM_FRONTEND = "music-assistant/frontend"
SERVER_PATCH_REF = "refs/heads/server-patches-20261118"
SERVER_PATCH_BASE = "fafd747c1560080e6bd10dd449706214e3356e34"
SERVER_PATCH_HEAD = "f078872605c79ba30e94239636f151f1e16409e2"
SERVER_PATCHES = (
    "fdb70ab6a2ababb075ad2a654444c877e706efee",
    "b2bb5d2cefe7d974529be3e133e78561d3a99645",
    "97f6d2607690013c621480991f28b4d64597ab97",
    "f1e76c1e2a81a133e97fa4e324671dfb8f485ad4",
    "0011e1c909d306db8c011fb7d57e1291cf976355",
    "8dc2d935b459a7952c26431b03b928753a58f2c5",
    "6c32fdc384b1ae65db301339a91cc0707d122729",
    "7d382df7cc03c97f29ef2c2b354a8e67a31b7a68",
    "581513fcf90d90bceb8b317fc89098b10e82397c",
    "ce2c2405d18e6bb1049a64510548bd0f61f5c84d",
    "a689b77b315cd4455d4464b6b078601a343e49b0",
    "5113cceba0ce685fab33cbaa29a158e5ebff4619",
    SERVER_PATCH_HEAD,
)
FRONTEND_PATCH_REF = "refs/heads/frontend-queue-next-burst"
FRONTEND_PATCH_BASE = "46cb842678865bdc9dcfa703c13d817e792806ed"
FRONTEND_PATCH_HEAD = "d092c4d2885a9acdcf34a088c924a85031deda3c"
FRONTEND_PATCHES = (
    "033d78bcf1a7e1dd63c3df172f21863e0e9a4424",
    FRONTEND_PATCH_HEAD,
)
NIGHTLY_TAG = re.compile(r"^\d+\.\d+\.\d+\.dev\d+$")
IMAGE_NAMESPACE = "music-assistant/server"
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class SyncError(RuntimeError):
    """Raised when a nightly cannot be qualified safely."""


@dataclass(frozen=True)
class Candidate:
    """Verified upstream inputs and deterministic patched source commits."""

    release_tag: str
    digest: str
    server_base: str
    frontend_version: str
    server_sha: str
    frontend_sha: str
    server_path: Path
    frontend_path: Path

    def output_values(self) -> dict[str, str]:
        """Return the serializable output fields passed from qualification to publishing."""
        return {
            "release_tag": self.release_tag,
            "digest": self.digest,
            "server_base": self.server_base,
            "frontend_version": self.frontend_version,
            "server_sha": self.server_sha,
            "frontend_sha": self.frontend_sha,
        }


def run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    """Run a command and return stdout, surfacing failures without leaking secrets."""
    child_env = dict(env) if env is not None else os.environ.copy()
    for key in (
        "GITHUB_TOKEN",
        "GITHUB_OUTPUT",
        "GITHUB_ENV",
        "GITHUB_PATH",
        "GITHUB_STEP_SUMMARY",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
    ):
        child_env.pop(key, None)
    result = subprocess.run(
        args,
        cwd=cwd,
        env=child_env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SyncError(f"Command failed ({result.returncode}): {args[0]} {args[1]}\n{detail}")
    return result.stdout.strip()


def http_json(url: str, *, headers: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
    """Read JSON from an HTTPS endpoint."""
    request_headers = {"User-Agent": "KlaysDe-HA-Store-sync", "Accept": "application/json"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response), dict(response.headers.items())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
        raise SyncError(f"Unable to read {url}: {err}") from err


def newest_nightly(releases: list[dict[str, Any]]) -> dict[str, Any]:
    """Select the newest published official server nightly release."""
    candidates = [
        item
        for item in releases
        if item.get("prerelease")
        and not item.get("draft")
        and NIGHTLY_TAG.fullmatch(str(item.get("tag_name", "")))
    ]
    if not candidates:
        raise SyncError("No published Music Assistant server nightly release was found")
    candidates.sort(key=lambda item: str(item.get("published_at", "")), reverse=True)
    return candidates[0]


def parse_frontend_dependency(pyproject_text: str) -> tuple[str, str]:
    """Return the paired frontend release version and wheel SHA-256."""
    project = tomllib.loads(pyproject_text)
    matches = [
        dependency
        for dependency in project.get("project", {}).get("dependencies", [])
        if dependency.lower().startswith("music-assistant-frontend @ ")
    ]
    if len(matches) != 1:
        raise SyncError("Server pyproject must declare one pinned music-assistant-frontend wheel")
    url = matches[0].split("@", 1)[1].strip()
    match = re.search(r"/releases/download/([^/]+)/music_assistant_frontend-([^/]+)-py3-none-any\.whl#sha256=([0-9a-f]{64})$", url)
    if not match or match.group(1) != match.group(2):
        raise SyncError("Could not resolve the exact frontend version and digest from server pyproject")
    return match.group(1), match.group(3)


def get_registry_token() -> str:
    """Obtain an anonymous read-only token for the official public GHCR package."""
    query = urllib.parse.urlencode({"scope": f"repository:{IMAGE_NAMESPACE}:pull"})
    data, _ = http_json(f"https://ghcr.io/token?{query}")
    token = data.get("token") or data.get("access_token")
    if not isinstance(token, str) or not token:
        raise SyncError("GHCR did not provide a public package read token")
    return token


def registry_manifest(reference: str, token: str) -> tuple[dict[str, Any], str]:
    """Fetch one official OCI manifest and return its JSON plus registry digest."""
    headers = {"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT}
    data, response_headers = http_json(
        f"https://ghcr.io/v2/{IMAGE_NAMESPACE}/manifests/{reference}", headers=headers
    )
    digest = response_headers.get("Docker-Content-Digest") or response_headers.get(
        "docker-content-digest"
    )
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SyncError(f"GHCR returned no valid immutable digest for {reference}")
    return data, digest


def official_dev_image() -> tuple[str, dict[str, str]]:
    """Resolve the :dev OCI index and confirm its platforms publish matching provenance."""
    token = get_registry_token()
    index, digest = registry_manifest("dev", token)
    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        raise SyncError("Official :dev image is not a multi-platform OCI index")
    platforms: dict[str, str] = {}
    for item in manifests:
        platform = item.get("platform", {})
        architecture = platform.get("architecture")
        if platform.get("os") == "linux" and architecture in {"amd64", "arm64"}:
            platforms[architecture] = item.get("digest", "")
    if set(platforms) != {"amd64", "arm64"}:
        raise SyncError("Official :dev image must publish both linux/amd64 and linux/arm64")

    provenance: dict[str, str] = {}
    for architecture, child_digest in platforms.items():
        child, _ = registry_manifest(child_digest, token)
        config_digest = child.get("config", {}).get("digest")
        if not isinstance(config_digest, str):
            raise SyncError(f"Official :dev {architecture} manifest has no image config")
        config, _ = http_json(
            f"https://ghcr.io/v2/{IMAGE_NAMESPACE}/blobs/{config_digest}",
            headers={"Authorization": f"Bearer {token}"},
        )
        labels = config.get("config", {}).get("Labels") or {}
        revision = labels.get("org.opencontainers.image.revision")
        version = labels.get("org.opencontainers.image.version")
        if not revision or not version:
            raise SyncError(f"Official :dev {architecture} image is missing version/revision labels")
        provenance[architecture] = f"{version}@{revision}"
    if len(set(provenance.values())) != 1:
        raise SyncError("Official :dev platform labels disagree: " + json.dumps(provenance, sort_keys=True))
    return digest, provenance


def git_init_source(path: Path, repository: str, reference: str) -> str:
    """Fetch a complete upstream tag history and return its commit."""
    path.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "--quiet"], cwd=path)
    run(["git", "remote", "add", "origin", f"https://github.com/{repository}.git"], cwd=path)
    # The publisher serves this repository to a local git fetch. Upload-pack
    # disables lazy fetching, so historical blobs must already be present.
    run(["git", "fetch", "--quiet", "origin", f"refs/tags/{reference}:refs/tags/{reference}"], cwd=path)
    commit = run(["git", "rev-parse", f"refs/tags/{reference}^{{commit}}"], cwd=path)
    run(["git", "checkout", "--quiet", "--detach", commit], cwd=path)
    run(["git", "config", "user.name", "Klay's HA Store release automation"], cwd=path)
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=path)
    return commit


def fetch_patch_ref(path: Path, ref: str, local_ref: str) -> str:
    """Fetch an advertised patch branch from the public fork and verify its ref name."""
    run(
        [
            "git",
            "fetch",
            "--quiet",
            "--no-tags",
            f"https://github.com/{SERVER_REPOSITORY}.git",
            f"{ref}:{local_ref}",
        ],
        cwd=path,
    )
    return run(["git", "rev-parse", local_ref], cwd=path)


def verify_patch_chain(path: Path, *, head: str, expected_head: str, commits: tuple[str, ...], base: str) -> None:
    """Require the advertised anchor to contain exactly the expected linear patch series."""
    if head != expected_head:
        raise SyncError(f"Patch branch moved: expected {expected_head}, got {head}")
    for index, commit in enumerate(commits):
        expected_parent = base if index == 0 else commits[index - 1]
        parents = run(["git", "show", "-s", "--format=%P", commit], cwd=path).split()
        if parents != [expected_parent]:
            raise SyncError(f"Patch anchor {commit} is no longer a linear child of {expected_parent}")


def require_ancestor(path: Path, ancestor: str, descendant: str, description: str) -> None:
    """Ensure the selected upstream release descends from the patch anchor base."""
    result = subprocess.run(
        ["git", "merge-base", ancestor, descendant],
        cwd=path,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode or result.stdout.strip() != ancestor:
        raise SyncError(f"Selected upstream release is not based on the expected {description}")


def cherry_pick_series(path: Path, commits: tuple[str, ...]) -> str:
    """Apply anchored commits deterministically; any conflict aborts the entire qualification."""
    for commit in commits:
        original_date = run(["git", "show", "-s", "--format=%cI", commit], cwd=path)
        env = os.environ.copy()
        env["GIT_COMMITTER_DATE"] = original_date
        run(["git", "cherry-pick", "--no-gpg-sign", "-x", commit], cwd=path, env=env)
    return run(["git", "rev-parse", "HEAD"], cwd=path)


def snapshot_runtime_source(path: Path) -> str:
    """Create a deterministic source commit without upstream Actions workflows or history."""
    source_head = run(["git", "rev-parse", "HEAD"], cwd=path)
    source_date = run(["git", "show", "-s", "--format=%cI", source_head], cwd=path)
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Music Assistant HA Store source",
        GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
        GIT_AUTHOR_DATE=source_date,
        GIT_COMMITTER_NAME="Music Assistant HA Store source",
        GIT_COMMITTER_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
        GIT_COMMITTER_DATE=source_date,
    )
    with tempfile.TemporaryDirectory(prefix="ma-store-source-index-") as directory:
        env["GIT_INDEX_FILE"] = str(Path(directory) / "index")
        run(["git", "read-tree", source_head], cwd=path, env=env)
        run(
            ["git", "rm", "--cached", "--quiet", "-r", "--ignore-unmatch", "--", ".github/workflows"],
            cwd=path,
            env=env,
        )
        tree = run(["git", "write-tree"], cwd=path, env=env)
        commit = run(
            ["git", "-c", "commit.gpgSign=false", "commit-tree", tree, "-m",
             f"chore(store): snapshot runtime source\n\nPatched source: {source_head}"],
            cwd=path,
            env=env,
        )
    run(["git", "checkout", "--quiet", "--detach", commit], cwd=path)
    return commit


def replace_exactly_once(text: str, pattern: str, replacement: str, description: str) -> str:
    """Replace one expected config value, failing closed if the file shape drifted."""
    if len(list(re.finditer(pattern, text, re.MULTILINE))) != 1:
        raise SyncError(f"Expected exactly one {description} declaration")
    return re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)


def bump_patch_version(version: str) -> str:
    """Increment the patch component of the channel-owned semantic version."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise SyncError(f"Unsupported channel version {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def channel_has_pins(channel_root: Path, digest: str, server_sha: str, frontend_sha: str) -> bool:
    """Check the three published pins without modifying the channel checkout."""
    dockerfile = (channel_root / "music_assistant" / "Dockerfile").read_text(encoding="utf-8")
    from_line = exactly_one(r"^FROM ghcr\.io/music-assistant/server:dev@sha256:([0-9a-f]{64})\s*$", dockerfile, "official :dev image pin")
    server_line = exactly_one(r"^ENV SERVER_REPO=KlaysDe/music-assistant-server@([0-9a-f]{40})\s*$", dockerfile, "server source pin")
    frontend_line = exactly_one(r"^ENV FRONTEND_REPO=KlaysDe/music-assistant-server@([0-9a-f]{40})\s*$", dockerfile, "frontend source pin")
    return (
        from_line, server_line, frontend_line
    ) == (digest.removeprefix("sha256:"), server_sha, frontend_sha)


def exactly_one(pattern: str, text: str, description: str) -> str:
    """Return one captured value and reject missing or duplicate declarations."""
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    if len(matches) != 1:
        raise SyncError(f"Expected exactly one {description} declaration")
    return matches[0].group(1)


def update_channel_files(channel_root: Path, digest: str, server_sha: str, frontend_sha: str) -> bool:
    """Atomically update only the add-on's version and pinned source/runtime lines."""
    config_path = channel_root / "music_assistant" / "config.yaml"
    dockerfile_path = channel_root / "music_assistant" / "Dockerfile"
    config = config_path.read_text(encoding="utf-8")
    dockerfile = dockerfile_path.read_text(encoding="utf-8")
    current_version = exactly_one(r'^version:\s*["\']?(\d+\.\d+\.\d+)["\']?\s*$', config, "channel version")
    if channel_has_pins(channel_root, digest, server_sha, frontend_sha):
        return False

    next_version = bump_patch_version(current_version)
    next_config = replace_exactly_once(
        config,
        r'^version:\s*["\']?\d+\.\d+\.\d+["\']?\s*$',
        f'version: "{next_version}"',
        "add-on version",
    )
    next_docker = replace_exactly_once(
        dockerfile,
        r"^FROM ghcr\.io/music-assistant/server:dev@sha256:[0-9a-f]{64}\s*$",
        f"FROM ghcr.io/music-assistant/server:dev@{digest}",
        "official :dev image pin",
    )
    next_docker = replace_exactly_once(
        next_docker,
        r"^ENV SERVER_REPO=KlaysDe/music-assistant-server@[0-9a-f]{40}\s*$",
        f"ENV SERVER_REPO=KlaysDe/music-assistant-server@{server_sha}",
        "server source pin",
    )
    next_docker = replace_exactly_once(
        next_docker,
        r"^ENV FRONTEND_REPO=KlaysDe/music-assistant-server@[0-9a-f]{40}\s*$",
        f"ENV FRONTEND_REPO=KlaysDe/music-assistant-server@{frontend_sha}",
        "frontend source pin",
    )
    config_path.write_text(next_config, encoding="utf-8", newline="\n")
    dockerfile_path.write_text(next_docker, encoding="utf-8", newline="\n")
    return True


def latest_server_release(token: str | None) -> dict[str, Any]:
    """Read the latest official published server nightly release."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    releases, _ = http_json(
        f"https://api.github.com/repos/{UPSTREAM_SERVER}/releases?per_page=100", headers=headers
    )
    if not isinstance(releases, list):
        raise SyncError("GitHub release API returned an invalid release list")
    return newest_nightly(releases)


def run_qualification(source: Path, frontend: Path) -> None:
    """Install the project test dependencies and run the scoped release gates."""
    run(["uv", "pip", "install", "--system", "-e", "."], cwd=source)
    run(["uv", "pip", "install", "--system", "-e", ".[test]"], cwd=source)
    all_dependencies_env = os.environ.copy()
    all_dependencies_env["URLLIB3_NO_OVERRIDE"] = "1"
    run(
        [
            "uv",
            "pip",
            "install",
            "--system",
            "--index-strategy",
            "unsafe-best-match",
            "-r",
            "requirements_all.txt",
        ],
        cwd=source,
        env=all_dependencies_env,
    )
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/providers/spotify/test_browse.py",
            "tests/providers/spotify/test_session.py",
            "tests/providers/spotify/test_session_fallback.py",
            "tests/providers/spotify/test_stats.py",
            "tests/controllers/music/test_spotify_playlist_touches.py",
            "tests/controllers/player_queues/test_next_burst.py",
            "tests/controllers/player_queues/test_play_index_elapsed.py",
        ],
        cwd=source,
    )
    run(["corepack", "pnpm", "install", "--frozen-lockfile"], cwd=frontend)
    run(
        [
            "corepack",
            "pnpm",
            "exec",
            "vitest",
            "run",
            "tests/views/EditProvider.test.ts",
            "tests/layouts/NextBtn.test.ts",
        ],
        cwd=frontend,
    )
    run(["corepack", "pnpm", "build"], cwd=frontend)


def push_qualified_update(
    channel_root: Path,
    server_path: Path,
    server_sha: str,
    frontend_path: Path,
    frontend_sha: str,
    github_token: str,
) -> None:
    """Publish the source refs and channel manifest together in one atomic push."""
    server_ref = f"refs/heads/ha-store-sources/server/{server_sha}"
    frontend_ref = f"refs/heads/ha-store-sources/frontend/{frontend_sha}"
    for ref, expected in ((server_ref, server_sha), (frontend_ref, frontend_sha)):
        existing = run(["git", "ls-remote", "--refs", "origin", ref], cwd=channel_root)
        if existing:
            actual, _name = existing.split(None, 1)
            if actual != expected:
                raise SyncError(f"Refusing to replace immutable source ref {ref}")
    server_local_ref = "refs/ha-store-staging/server"
    frontend_local_ref = "refs/ha-store-staging/frontend"
    try:
        run(["git", "fetch", "--no-tags", str(server_path), f"{server_sha}:{server_local_ref}"], cwd=channel_root)
        run(["git", "fetch", "--no-tags", str(frontend_path), f"{frontend_sha}:{frontend_local_ref}"], cwd=channel_root)
        channel_head = run(["git", "rev-parse", "HEAD"], cwd=channel_root)
        push_env = os.environ.copy()
        push_env.pop("GITHUB_TOKEN", None)
        push_env["GIT_CONFIG_COUNT"] = "1"
        push_env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        encoded_token = base64.b64encode(f"x-access-token:{github_token}".encode()).decode()
        push_env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {encoded_token}"
        run(
            [
                "git",
                "push",
                "--atomic",
                "origin",
                f"{server_local_ref}:{server_ref}",
                f"{frontend_local_ref}:{frontend_ref}",
                f"{channel_head}:refs/heads/ha-store",
            ],
            cwd=channel_root,
            env=push_env,
        )
    finally:
        run(["git", "update-ref", "-d", server_local_ref], cwd=channel_root)
        run(["git", "update-ref", "-d", frontend_local_ref], cwd=channel_root)


def resolve_candidate(work_root: Path, release_token: str | None = None) -> Candidate:
    """Resolve official upstream inputs and deterministically replay the patch anchors."""
    release = latest_server_release(release_token)
    release_tag = str(release["tag_name"])
    digest, platform_info = official_dev_image()

    server_path = work_root / "server"
    server_base = git_init_source(server_path, UPSTREAM_SERVER, release_tag)
    official_provenance = set(platform_info.values())
    expected_provenance = f"{release_tag}@{server_base}"
    if official_provenance != {expected_provenance}:
        raise SyncError(
            f"Official :dev image does not match latest nightly {expected_provenance}: "
            + ", ".join(sorted(official_provenance))
        )

    server_pyproject = run(["git", "show", f"{server_base}:pyproject.toml"], cwd=server_path)
    frontend_version, _wheel_sha = parse_frontend_dependency(server_pyproject)
    frontend_path = work_root / "frontend"
    frontend_base = git_init_source(frontend_path, UPSTREAM_FRONTEND, frontend_version)

    server_patch_head = fetch_patch_ref(
        server_path, SERVER_PATCH_REF, "refs/remotes/channel/server-patches"
    )
    require_ancestor(server_path, SERVER_PATCH_BASE, server_base, "server patch anchor")
    verify_patch_chain(
        server_path,
        head=server_patch_head,
        expected_head=SERVER_PATCH_HEAD,
        commits=SERVER_PATCHES,
        base=SERVER_PATCH_BASE,
    )
    cherry_pick_series(server_path, SERVER_PATCHES)
    server_result = snapshot_runtime_source(server_path)

    frontend_patch_head = fetch_patch_ref(
        frontend_path, FRONTEND_PATCH_REF, "refs/remotes/channel/frontend-patches"
    )
    require_ancestor(frontend_path, FRONTEND_PATCH_BASE, frontend_base, "frontend patch anchor")
    verify_patch_chain(
        frontend_path,
        head=frontend_patch_head,
        expected_head=FRONTEND_PATCH_HEAD,
        commits=FRONTEND_PATCHES,
        base=FRONTEND_PATCH_BASE,
    )
    cherry_pick_series(frontend_path, FRONTEND_PATCHES)
    frontend_result = snapshot_runtime_source(frontend_path)

    return Candidate(
        release_tag=release_tag,
        digest=digest,
        server_base=server_base,
        frontend_version=frontend_version,
        server_sha=server_result,
        frontend_sha=frontend_result,
        server_path=server_path,
        frontend_path=frontend_path,
    )


def write_candidate_outputs(candidate: Candidate) -> None:
    """Write the tested candidate fields as GitHub Actions step outputs."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        raise SyncError("Qualification requires the GitHub Actions output file")
    with Path(output_file).open("a", encoding="utf-8", newline="\n") as output:
        for key, value in candidate.output_values().items():
            if "\n" in value or "\r" in value:
                raise SyncError(f"Invalid newline in qualified candidate field {key}")
            output.write(f"{key}={value}\n")


def qualify(work_root: Path) -> Candidate:
    """Replay and test a candidate without repository write credentials."""
    if not os.environ.get("GITHUB_ACTIONS"):
        raise SyncError("Qualification is allowed only from the GitHub Actions workflow")
    candidate = resolve_candidate(work_root, os.environ.get("GITHUB_TOKEN"))
    run_qualification(candidate.server_path, candidate.frontend_path)
    write_candidate_outputs(candidate)
    print(
        f"Qualification passed: {candidate.release_tag} ({candidate.digest}); "
        f"server {candidate.server_sha}, frontend {candidate.frontend_sha}."
    )
    return candidate


def publish_qualified(
    channel_root: Path,
    work_root: Path,
    expected: dict[str, str],
    github_token: str,
) -> bool:
    """Re-resolve the candidate, compare it to qualification, then atomically publish."""
    if not os.environ.get("GITHUB_ACTIONS"):
        raise SyncError("Publishing is allowed only from the GitHub Actions workflow")
    if os.environ.get("GITHUB_REPOSITORY", "").lower() != SERVER_REPOSITORY.lower():
        raise SyncError(f"Publishing is restricted to {SERVER_REPOSITORY}")
    if not github_token:
        raise SyncError("Publishing requires the workflow-scoped GITHUB_TOKEN")

    candidate = resolve_candidate(work_root, github_token)
    if expected != candidate.output_values():
        raise SyncError("Upstream inputs changed after qualification; refusing to publish an untested candidate")
    if channel_has_pins(channel_root, candidate.digest, candidate.server_sha, candidate.frontend_sha):
        print(f"Already qualified: {candidate.release_tag} ({candidate.digest})")
        return False

    update_channel_files(channel_root, candidate.digest, candidate.server_sha, candidate.frontend_sha)
    run(["git", "add", "--", "music_assistant/config.yaml", "music_assistant/Dockerfile"], cwd=channel_root)
    run(
        [
            "git",
            "-c",
            "user.name=github-actions[bot]",
            "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit",
            "-m",
            f"chore(store): sync qualified {candidate.release_tag}",
        ],
        cwd=channel_root,
    )
    push_qualified_update(
        channel_root,
        candidate.server_path,
        candidate.server_sha,
        candidate.frontend_path,
        candidate.frontend_sha,
        github_token,
    )
    print(
        f"Published qualified {candidate.release_tag}: {candidate.server_sha} "
        f"{candidate.frontend_sha} {candidate.digest}"
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-root", type=Path, default=Path.cwd())
    parser.add_argument("mode", choices=("qualify", "publish"))
    parser.add_argument("--release-tag")
    parser.add_argument("--digest")
    parser.add_argument("--server-base")
    parser.add_argument("--frontend-version")
    parser.add_argument("--server-sha")
    parser.add_argument("--frontend-sha")
    args = parser.parse_args()
    channel_root = args.channel_root.resolve()
    if not (channel_root / ".git").exists():
        raise SyncError(f"Channel checkout is not a git worktree: {channel_root}")
    runner_temp = os.environ.get("RUNNER_TEMP") or None
    with tempfile.TemporaryDirectory(prefix="ha-store-sync-", dir=runner_temp) as temporary:
        work_root = Path(temporary)
        if args.mode == "qualify":
            qualify(work_root)
            return 0

        expected = {
            "release_tag": args.release_tag,
            "digest": args.digest,
            "server_base": args.server_base,
            "frontend_version": args.frontend_version,
            "server_sha": args.server_sha,
            "frontend_sha": args.frontend_sha,
        }
        if any(value is None for value in expected.values()):
            raise SyncError("Publishing requires all qualified candidate fields")
        github_token = os.environ.pop("GITHUB_TOKEN", None)
        publish_qualified(channel_root, work_root, expected, github_token or "")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as error:
        message = f"Release qualification stopped: {error}"
        print(message, file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true" and sys.argv[1:2] == ["publish"]:
            summary = message.splitlines()[0].replace("%", "%25")
            print(f"::error::{summary}", file=sys.stderr)
        raise SystemExit(1) from error
