"""Spotify Web API session with its own client id, token and rate limit."""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.errors import (
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from orjson import JSONDecodeError

from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import (
    MAX_RETRY_AFTER,
    RequestPriority,
    ThrottlerManager,
    current_priority,
    current_throttle_delay,
    parse_retry_after,
    throttle_with_retries,
)

from .request_stats import DEVELOPER_LABEL, MUSIC_ASSISTANT_LABEL, endpoint_label

if TYPE_CHECKING:
    import logging

    from music_assistant.mass import MusicAssistant

    from .request_stats import SpotifyRequestStats

API_URL = "https://api.spotify.com/v1"


class SpotifySession:
    """
    Requests to the Spotify Web API on behalf of one client id.

    Spotify rate limits per client id, so each session has a throttler of its own.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        logger: logging.Logger,
        name: str,
        throttler: ThrottlerManager,
        get_auth: Callable[[], Awaitable[dict[str, Any]]],
        on_unauthorized: Callable[[], None],
        fallback_for_playback: bool = False,
        request_stats: SpotifyRequestStats | None = None,
    ) -> None:
        """
        Initialize the session.

        :param mass: The Music Assistant instance.
        :param logger: Logger for the requests of this session.
        :param name: Name of the session, for logging and diagnostics.
        :param throttler: Throttler that paces the requests of this session.
        :param get_auth: Returns a valid access token of this session.
        :param on_unauthorized: Called when Spotify rejects the access token of this session.
        :param fallback_for_playback: Whether another session serves playback while this one is
            rate limited, so a playback request gives up on a limit at once.
        :param request_stats: Optional request history for this provider instance.
        """
        self.mass = mass
        self.name = name
        self.token_label = DEVELOPER_LABEL if name == "dev" else MUSIC_ASSISTANT_LABEL
        self.logger = logger.getChild(self.token_label)
        self.throttler = throttler
        self._get_auth = get_auth
        self._on_unauthorized = on_unauthorized
        self._fallback_for_playback = fallback_for_playback
        self._request_stats = request_stats

    @throttle_with_retries
    async def get(
        self, /, endpoint: str, *, auth_info: dict[str, Any] | None = None, **params: Any
    ) -> dict[str, Any]:
        """
        Get data from the api.

        :param endpoint: API endpoint to call.
        :param auth_info: Access token to use instead of the one of this session.
        :param params: Query parameters of the request.
        :raises MediaNotFoundError: When Spotify does not have, or does not share, the item.
        """
        params["market"] = "from_token"
        params["country"] = "from_token"
        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        headers = {"Accept-Language": f"{locale}, {language};q=0.9, *;q=0.5"}
        async with self._request(
            "GET",
            endpoint,
            auth_info=auth_info,
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as response:
            if response.status in (400, 403, 404):
                try:
                    error = await response.json(loads=json_loads)
                    message = error.get("error", {}).get("message") or response.reason
                except aiohttp.ContentTypeError, JSONDecodeError:
                    message = (await response.text()) or response.reason

                self.logger.debug(
                    "Spotify API error: endpoint=%s, status=%s, reason=%s, message=%s",
                    endpoint_label("GET", endpoint),
                    response.status,
                    response.reason,
                    message,
                )

                raise MediaNotFoundError(f"{endpoint} not found")

            response.raise_for_status()
            result: dict[str, Any] = await response.json(loads=json_loads)
            if etag := response.headers.get("ETag"):
                result["etag"] = etag
            return result

    @throttle_with_retries
    async def delete(
        self,
        /,
        endpoint: str,
        data: Any = None,
        *,
        auth_info: dict[str, Any] | None = None,
        **params: Any,
    ) -> None:
        """
        Delete data from the api.

        :param endpoint: API endpoint to call.
        :param data: JSON body of the request.
        :param auth_info: Token to use, the session's own token when omitted.
        :param params: Query parameters of the request.
        """
        async with self._request(
            "DELETE", endpoint, auth_info=auth_info, params=params, json=data, ssl=True
        ) as response:
            response.raise_for_status()

    @throttle_with_retries
    async def put(
        self,
        /,
        endpoint: str,
        data: Any = None,
        *,
        auth_info: dict[str, Any] | None = None,
        **params: Any,
    ) -> None:
        """
        Put data on the api.

        :param endpoint: API endpoint to call.
        :param data: JSON body of the request.
        :param auth_info: Token to use, the session's own token when omitted.
        :param params: Query parameters of the request.
        """
        async with self._request(
            "PUT", endpoint, auth_info=auth_info, params=params, json=data, ssl=True
        ) as response:
            response.raise_for_status()

    @throttle_with_retries
    async def post(
        self,
        /,
        endpoint: str,
        data: Any = None,
        want_result: bool = True,
        *,
        auth_info: dict[str, Any] | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """
        Post data on the api.

        :param endpoint: API endpoint to call.
        :param data: JSON body of the request.
        :param want_result: Return the response body, an empty dict when False.
        :param auth_info: Token to use, the session's own token when omitted.
        :param params: Query parameters of the request.
        """
        async with self._request(
            "POST", endpoint, auth_info=auth_info, params=params, json=data, ssl=True
        ) as response:
            response.raise_for_status()
            if not want_result:
                return {}
            result: dict[str, Any] = await response.json(loads=json_loads)
            return result

    @asynccontextmanager
    async def _request(
        self,
        method: str,
        endpoint: str,
        auth_info: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[aiohttp.ClientResponse]:
        """
        Send a request and yield its response, raising for the statuses every method shares.

        :param method: HTTP method of the request.
        :param endpoint: API endpoint to call.
        :param auth_info: Access token to use instead of the one of this session.
        :param headers: Extra headers of the request.
        :param kwargs: Extra arguments for the aiohttp request.
        """
        if not auth_info:
            auth_info = await self._get_auth()
        headers = {**(headers or {}), "Authorization": f"Bearer {auth_info['access_token']}"}
        route = endpoint_label(method, endpoint)
        self.logger.debug("Spotify Web API %s %s started", self.token_label, route)
        started = time.monotonic()
        status = 0
        try:
            async with self.mass.http_session.request(
                method, f"{API_URL}/{endpoint}", headers=headers, **kwargs
            ) as response:
                status = response.status
                backoff_time = (
                    parse_retry_after(response.headers.get("Retry-After")) if status == 429 else 0
                )
                self._record_attempt(route, status, started, backoff_time)
                if status == 429:
                    if self._fallback_for_playback and current_priority() is RequestPriority.HIGH:
                        # playback does not sit out a limit while another session can serve it
                        self.throttler.set_cooldown(
                            max(backoff_time, self.throttler.initial_backoff)
                        )
                        raise RetriesExhausted(
                            "Spotify Rate Limiter", translation_key=RateLimited.translation_key
                        )
                    raise RateLimited("Spotify Rate Limiter", backoff_time=backoff_time)
                # handle token expired and server errors through the existing retry policy
                if status == 401:
                    self._on_unauthorized()
                    raise ResourceTemporarilyUnavailable("Token expired", backoff_time=1)
                if status in (502, 503):
                    raise ResourceTemporarilyUnavailable(backoff_time=30)
                yield response
        except aiohttp.ClientError, TimeoutError:
            if not status:
                self._record_attempt(route, status, started, 0)
            raise

    def _record_attempt(self, route: str, status: int, started: float, retry_after: int) -> None:
        """Log and count an attempt once, before any retry or fallback."""
        elapsed = time.monotonic() - started
        wait = current_throttle_delay()
        self.logger.debug(
            "Spotify Web API %s %s → %s (%.0f ms response, %.0f ms throttle wait, %s priority)",
            self.token_label,
            route,
            status or "transport error",
            elapsed * 1000,
            wait * 1000,
            current_priority().name.lower(),
        )
        if status == 429:
            self.logger.info(
                "%s %s: HTTP 429, Retry-After %ss", self.token_label, route, retry_after
            )
        if self._request_stats is not None:
            self._request_stats.record(
                self.token_label, route, status, wait, elapsed, min(retry_after, MAX_RETRY_AFTER)
            )
