"""Serve the Spotify request dashboard opened from provider settings."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from aiohttp import web
from music_assistant_models.auth import Scope
from music_assistant_models.errors import ActionUnavailable

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_client_id,
    has_scope,
)
from music_assistant.helpers.provider_access import source_owner

from .request_stats import STATS_WINDOWS

if TYPE_CHECKING:
    from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler

    from .provider import SpotifyProvider

_GRANT_LIFETIME: Final = 3600
_MAX_GRANTS: Final = 16
_HTML_PATH: Final = Path(__file__).with_name("stats.html")
_HEADERS: Final = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class SpotifyStatsPage:
    """Expose a provider-specific dashboard to the browser that opened it."""

    def __init__(self, provider: SpotifyProvider) -> None:
        """Prepare the dashboard routes for this provider."""
        self._provider = provider
        self._webserver = provider.mass.webserver
        self._path = f"/spotify/stats/{provider.instance_id}/"
        self._grants: dict[str, tuple[float, WebsocketClientHandler]] = {}
        self._unregister: list[Callable[[], None]] = []

    def start(self) -> None:
        """Register the dashboard and its data route."""
        for suffix, method, handler in (
            ("", "GET", self._page),
            ("data", "POST", self._data),
        ):
            self._unregister.append(
                self._webserver.register_dynamic_route(self._path + suffix, handler, method)
            )

    def close(self) -> None:
        """Withdraw the routes and revoke their access grants."""
        for unregister in self._unregister:
            unregister()
        self._unregister.clear()
        self._grants.clear()

    def open_url(self) -> str:
        """Create a dashboard URL for the authenticated settings connection."""
        client_id = get_current_client_id()
        client = next((c for c in self._webserver.clients if c.client_id == client_id), None)
        if not self._unregister or client is None or not self._can_view(client):
            raise ActionUnavailable("Open Spotify stats from the Music Assistant settings page.")
        base_url = client.base_url or f"{client.request.scheme}://{client.request.host}"
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ActionUnavailable("Could not determine the address of the Spotify stats page.")
        now = time.monotonic()
        self._grants = {
            key: grant
            for key, grant in self._grants.items()
            if grant[0] > now and grant[1] in self._webserver.clients
        }
        if len(self._grants) >= _MAX_GRANTS:
            del self._grants[next(iter(self._grants))]
        key = secrets.token_urlsafe(32)
        self._grants[key] = (now + _GRANT_LIFETIME, client)
        return f"{base_url.rstrip('/')}{self._path}#{key}"

    def _can_view(self, client: WebsocketClientHandler) -> bool:
        """Require the same provider management permission as the settings action."""
        user = client.authenticated_user
        return user is not None and (
            has_scope(user, Scope.CONFIG_PROVIDERS_WRITE)
            or (
                has_scope(user, Scope.CONFIG_PROVIDERS_OWN)
                and source_owner(self._provider.mass, self._provider.instance_id) == user.user_id
            )
        )

    async def _page(self, request: web.Request) -> web.Response:
        """Serve the dashboard shell without embedding any request data."""
        nonce = secrets.token_urlsafe(24)
        html = await asyncio.to_thread(_HTML_PATH.read_text, encoding="utf-8")
        csp = (
            "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'"
        )
        return web.Response(
            text=html.replace("__STATS_NONCE__", nonce),
            content_type="text/html",
            headers={**_HEADERS, "Content-Security-Policy": csp},
        )

    async def _data(self, request: web.Request) -> web.Response:
        """Return request metrics to the authorized dashboard tab."""
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        grant = self._grants.get(bearer)
        if (
            grant is None
            or grant[0] <= time.monotonic()
            or grant[1] not in self._webserver.clients
            or not self._can_view(grant[1])
        ):
            raise web.HTTPUnauthorized(headers=_HEADERS)
        try:
            body = await request.json()
        except ValueError, UnicodeError:
            raise web.HTTPBadRequest(headers=_HEADERS) from None
        minutes = body.get("window_minutes") if isinstance(body, dict) else None
        if type(minutes) is not int or minutes not in STATS_WINDOWS:
            raise web.HTTPBadRequest(headers=_HEADERS)
        return web.json_response(self._provider.request_stats_snapshot(minutes), headers=_HEADERS)
