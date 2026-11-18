"""Tests for Spotify API request history and dashboard access."""

from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import UserRole
from music_assistant_models.errors import ActionUnavailable

from music_assistant.helpers.api import redact_json_secrets
from music_assistant.providers.spotify import request_stats, stats_page


def test_stats_roll_up_requests_and_expire_old_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Separate time ranges, upstream limits and time spent in Music Assistant's gate."""
    now = 90_000
    monkeypatch.setattr(request_stats.time, "time", lambda: now)
    stats = request_stats.SpotifyRequestStats()
    stats.record("music-assistant", "GET /me/playlists", 200, 1.5, 0.3)
    now += 60
    stats.record("music-assistant", "GET /me/playlists", 429, 0, 0.2, 30)

    result = stats.snapshot(1440)
    assert result["bucket_seconds"] == 900
    assert result["rows"] == [
        {
            "time": 90_000,
            "token": "music-assistant",
            "endpoint": "GET /me/playlists",
            "requests": 2,
            "rate_limited": 1,
            "errors": 0,
            "paced": 1,
            "wait_ms": 1500,
            "response_ms": 500,
            "max_retry_after": 30,
        }
    ]
    now += 15 * 60
    assert stats.snapshot(15)["rows"] == []
    now += 86400
    assert stats.snapshot(1440)["rows"] == []


async def test_stats_page_requires_grant_from_current_config_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep Ingress URLs intact and reject the grant after the settings client disconnects."""
    provider = MagicMock(instance_id="spotify--test")
    provider.request_stats_snapshot.return_value = {"rows": []}
    webserver = provider.mass.webserver
    client = MagicMock(client_id="calling-client", base_url="https://ha.example/ingress-prefix")
    client.authenticated_user.role = UserRole.ADMIN
    webserver.clients = {client}
    monkeypatch.setattr(stats_page, "get_current_client_id", lambda: client.client_id)
    page = stats_page.SpotifyStatsPage(provider)
    page.start()

    url = urlsplit(page.open_url())
    assert url.scheme == "https"
    assert url.netloc == "ha.example"
    assert url.path == "/ingress-prefix/spotify/stats/spotify--test/"
    assert url.query == ""
    assert url.fragment
    assert url.fragment not in redact_json_secrets(f'{{"open_url": "{url.geturl()}"}}')
    path, handler, method = webserver.register_dynamic_route.call_args.args
    assert (path, method) == ("/spotify/stats/spotify--test/data", "POST")
    request = make_mocked_request(method, path)
    with pytest.raises(web.HTTPUnauthorized):
        await handler(request)
    request = MagicMock(headers={"Authorization": f"Bearer {url.fragment}"})
    request.json = AsyncMock(return_value={"window_minutes": 15})
    response = await handler(request)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    provider.request_stats_snapshot.assert_called_once_with(15)

    webserver.clients.clear()
    with pytest.raises(web.HTTPUnauthorized):
        await handler(request)
    with pytest.raises(ActionUnavailable):
        page.open_url()
    page.close()
