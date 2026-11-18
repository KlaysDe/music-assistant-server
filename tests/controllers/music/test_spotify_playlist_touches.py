"""Tests for the Spotify playlist observation API."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from music_assistant_models.media_items import Playlist, ProviderMapping, Track

from music_assistant.controllers.music.media.playlists import PlaylistController
from music_assistant.models.music_provider import MusicProvider

OWNER = User(user_id="owner", username="owner", role=UserRole.USER)
MEMBER = User(user_id="member", username="member", role=UserRole.USER)
CURRENT_USER = "music_assistant.controllers.music.media.playlists.get_current_user"


@pytest.fixture
def playlists() -> PlaylistController:
    """Construct a playlist controller with isolated server collaborators."""
    return PlaylistController(MagicMock())


async def test_spotify_playlist_access_journal_is_scoped_and_cursor_based(
    playlists: PlaylistController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the current user sees an exact Spotify playlist's first-page access."""
    provider = MagicMock(spec=MusicProvider, domain="spotify", instance_id="spotify--owner")
    monkeypatch.setattr(playlists, "_playlist_provider", MagicMock(return_value=provider))
    monkeypatch.setattr(
        "music_assistant.controllers.music.media.playlists.cached_listing",
        AsyncMock(return_value=[]),
    )
    visible = MagicMock(return_value=[provider.instance_id])
    monkeypatch.setattr(
        "music_assistant.controllers.music.media.playlists.visible_music_sources", visible
    )
    first_id, second_id = "A" * 22, "B" * 22
    with patch(CURRENT_USER, return_value=OWNER):
        await playlists.tracks(first_id, provider.instance_id, offset=100)
        assert (await playlists.recent_touches())["playlists"] == []
        await playlists.tracks(first_id, provider.instance_id)
        first = await playlists.recent_touches()
        assert first["playlists"] == [
            {"provider_instance": provider.instance_id, "item_id": first_id}
        ]
        cursor = first["cursor"]
        assert isinstance(cursor, str)
        assert (await playlists.recent_touches(cursor))["playlists"] == []
        await playlists.tracks(first_id, provider.instance_id)
        await playlists.tracks(second_id, provider.instance_id)
        await playlists.tracks(first_id, provider.instance_id)
        observed = await playlists.recent_touches(cursor)
        assert observed["playlists"] == [
            {"provider_instance": provider.instance_id, "item_id": second_id},
            {"provider_instance": provider.instance_id, "item_id": first_id},
        ]
        assert await playlists.recent_touches("prior-server:18") == observed
        visible.return_value = []
        assert (await playlists.recent_touches(cursor))["playlists"] == []
    with patch(CURRENT_USER, return_value=MEMBER):
        assert (await playlists.recent_touches(cursor))["playlists"] == []


async def test_provider_playlist_export_uses_exact_instance_and_propagates_page_failure(
    playlists: PlaylistController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable provider page cannot produce a successful partial export."""
    playlist_id, instance_id = "C" * 22, "spotify--owner"
    provider = MagicMock(spec=MusicProvider, domain="spotify", instance_id=instance_id)
    provider.get_playlist = AsyncMock(
        return_value=Playlist(
            item_id=playlist_id,
            provider=instance_id,
            name="Spotify playlist",
            owner="Spotify",
            is_editable=False,
            provider_mappings=set(),
        )
    )
    resolve = MagicMock(return_value=provider)
    monkeypatch.setattr(playlists, "_playlist_provider", resolve)
    track = Track(
        item_id="D" * 22,
        provider=instance_id,
        name="Spotify track",
        provider_mappings={
            ProviderMapping(
                item_id="D" * 22, provider_domain="spotify", provider_instance=instance_id
            )
        },
    )
    track_page = AsyncMock(
        side_effect=([], [track], ResourceTemporarilyUnavailable("Spotify unavailable"))
    )
    provider.get_playlist_tracks = track_page
    monkeypatch.setattr(playlists, "_add_audio_metadata", AsyncMock())

    with patch(CURRENT_USER, return_value=OWNER):
        exported = await playlists.export_provider_playlist(playlist_id, instance_id)
        assert isinstance(exported["m3u_data"], str)
        assert exported["m3u_data"].startswith("#EXTM3U")
        with pytest.raises(ResourceTemporarilyUnavailable):
            await playlists.export_provider_playlist(playlist_id, instance_id)
        assert (await playlists.recent_touches())["playlists"] == []
    provider.get_playlist.assert_awaited_with(playlist_id)
    assert all(args == (instance_id, True) for args, _ in resolve.call_args_list)


async def test_spotify_edit_is_observed_without_opening_playlist(
    playlists: PlaylistController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful item removal is visible to the playlist journal reader."""
    playlist_id, instance_id = "E" * 22, "spotify--owner"
    provider = MagicMock(spec=MusicProvider, domain="spotify", instance_id=instance_id)
    provider.supported_features = {ProviderFeature.PLAYLIST_TRACKS_EDIT}
    provider.remove_playlist_tracks = AsyncMock()
    playlists.mass.music.resolve_visible_provider.return_value = provider
    playlist = Playlist(
        item_id="42",
        provider="library",
        name="Spotify playlist",
        owner="Spotify",
        is_editable=True,
        provider_mappings=set(),
    )
    monkeypatch.setattr(playlists, "_task_user", AsyncMock(return_value=OWNER))
    monkeypatch.setattr(playlists, "_get_editable_library_item", AsyncMock(return_value=playlist))
    monkeypatch.setattr(playlists, "_get_visible_library_item", AsyncMock(return_value=playlist))
    monkeypatch.setattr(
        playlists, "_select_provider_id", MagicMock(return_value=(instance_id, playlist_id))
    )
    monkeypatch.setattr(playlists, "_request_metadata_refresh", AsyncMock())
    monkeypatch.setattr(
        "music_assistant.controllers.music.media.playlists.visible_music_sources",
        MagicMock(return_value=[instance_id]),
    )
    monkeypatch.setattr(
        "music_assistant.controllers.music.media.playlists.set_current_user", MagicMock()
    )
    with patch(CURRENT_USER, return_value=OWNER):
        await playlists._handle_remove_playlist_tracks(playlist.item_id, (1,), OWNER.user_id)
        assert (await playlists.recent_touches())["playlists"] == [
            {"provider_instance": instance_id, "item_id": playlist_id},
        ]
    provider.remove_playlist_tracks.assert_awaited_once_with(playlist_id, (1,))
