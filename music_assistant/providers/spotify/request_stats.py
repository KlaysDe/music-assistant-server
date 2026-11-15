"""Bounded, in-memory diagnostics for Spotify Web API traffic."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Final

MUSIC_ASSISTANT_LABEL: Final = "music-assistant"
DEVELOPER_LABEL: Final = "developer-token"
STATS_WINDOWS: Final = {15: 60, 60: 60, 360: 300, 1440: 900}
_MINUTE: Final = 60
_RETENTION_SECONDS: Final = 1440 * _MINUTE

_STATIC_PATHS: Final = {
    "browse/categories",
    "browse/new-releases",
    "me",
    "me/albums",
    "me/audiobooks",
    "me/following",
    "me/library",
    "me/playlists",
    "me/shows",
    "me/tracks",
    "search",
}
_ITEM_PATHS: Final = {
    "albums": {"tracks"},
    "artists": {"albums", "top-tracks"},
    "audiobooks": {"chapters"},
    "episodes": set(),
    "playlists": {"items", "tracks"},
    "shows": {"episodes"},
    "tracks": set(),
}


def endpoint_label(method: str, endpoint: str) -> str:
    """Group requests by method and route without retaining IDs or query parameters."""
    path = endpoint.partition("?")[0].strip("/")
    if path in _STATIC_PATHS:
        return f"{method} /{path}"
    parts = path.split("/")
    if len(parts) == 4 and parts[:2] == ["browse", "categories"] and parts[3] == "playlists":
        return f"{method} /browse/categories/:id/playlists"
    resource = parts[0]
    if resource in _ITEM_PATHS:
        if len(parts) == 2:
            return f"{method} /{resource}/:id"
        if len(parts) == 3 and parts[2] in _ITEM_PATHS[resource]:
            return f"{method} /{resource}/:id/{parts[2]}"
    return f"{method} /other"


@dataclass(slots=True)
class _Counters:
    requests: int = 0
    rate_limited: int = 0
    errors: int = 0
    paced: int = 0
    wait_ms: int = 0
    response_ms: int = 0
    max_retry_after: int = 0

    def merge(self, other: _Counters) -> None:
        """Add another bucket's counters."""
        self.requests += other.requests
        self.rate_limited += other.rate_limited
        self.errors += other.errors
        self.paced += other.paced
        self.wait_ms += other.wait_ms
        self.response_ms += other.response_ms
        self.max_retry_after = max(self.max_retry_after, other.max_retry_after)


class SpotifyRequestStats:
    """Keep per-minute request totals for the last 24 hours."""

    def __init__(self) -> None:
        """Initialize the rolling request counters."""
        self.started_at = int(time.time())
        self._last_minute = 0
        self._minutes: dict[int, dict[tuple[str, str], _Counters]] = {}

    def record(
        self,
        token: str,
        endpoint: str,
        status: int,
        wait: float,
        elapsed: float,
        retry_after: int = 0,
    ) -> None:
        """Count a completed HTTP attempt, using status zero for transport errors."""
        minute = self._prune(int(time.time()))
        counters = self._minutes.setdefault(minute, {}).setdefault((token, endpoint), _Counters())
        counters.requests += 1
        counters.rate_limited += status == 429
        counters.errors += status == 0 or (status >= 400 and status != 429)
        counters.paced += wait > 0
        counters.wait_ms += round(wait * 1000)
        counters.response_ms += round(elapsed * 1000)
        counters.max_retry_after = max(counters.max_retry_after, retry_after)

    def snapshot(self, window_minutes: int) -> dict[str, int | list[dict[str, str | int]]]:
        """Return the selected time range, grouped into chart buckets."""
        now = int(time.time())
        minute = self._prune(now)
        first = minute - (window_minutes - 1) * _MINUTE
        interval = STATS_WINDOWS[window_minutes]
        grouped: dict[tuple[int, str, str], _Counters] = {}
        for timestamp, counters in self._minutes.items():
            if timestamp < first:
                continue
            bucket = timestamp - timestamp % interval
            for (token, endpoint), counts in counters.items():
                grouped.setdefault((bucket, token, endpoint), _Counters()).merge(counts)
        return {
            "started_at": self.started_at,
            "observed_at": now,
            "window_start": first,
            "bucket_seconds": interval,
            "rows": [
                {"time": timestamp, "token": token, "endpoint": endpoint, **asdict(counts)}
                for (timestamp, token, endpoint), counts in sorted(grouped.items())
            ],
        }

    def _prune(self, now: int) -> int:
        """Drop minute buckets outside the retention window."""
        minute = now // _MINUTE * _MINUTE
        if minute != self._last_minute:
            cutoff = minute - _RETENTION_SECONDS
            self._minutes = {
                key: val for key, val in self._minutes.items() if cutoff < key <= minute
            }
            self._last_minute = minute
        return minute
