"""Keep recent playlist accesses available to the same authenticated user."""

from collections import OrderedDict
from dataclasses import dataclass
from uuid import uuid4

MAX_PLAYLIST_TOUCHES = 256


@dataclass(frozen=True, slots=True)
class PlaylistTouch:
    """An exact provider playlist recently used by the client."""

    provider_instance: str
    item_id: str
    library_id: str | None


class PlaylistTouches:
    """Bound and coalesce one user's playlist accesses by provider and item ID."""

    def __init__(self) -> None:
        """Initialize the journal with a fresh epoch."""
        self._epoch = uuid4().hex
        self._sequence = 0
        self._touches: OrderedDict[tuple[str, str], tuple[int, PlaylistTouch]] = OrderedDict()

    def record(self, touch: PlaylistTouch) -> None:
        """Record the latest access for this playlist."""
        self._sequence += 1
        key = (touch.provider_instance, touch.item_id)
        self._touches[key] = (self._sequence, touch)
        self._touches.move_to_end(key)
        if len(self._touches) > MAX_PLAYLIST_TOUCHES:
            self._touches.popitem(last=False)

    def since(self, cursor: str | None) -> tuple[str, list[PlaylistTouch]]:
        """
        Return touches newer than the given cursor, or all retained touches after a restart.

        :param cursor: The cursor returned on the previous poll.
        """
        epoch, _, sequence = (cursor or "").partition(":")
        after = 0
        if epoch == self._epoch and sequence.isascii() and sequence.isdecimal():
            if len(sequence) <= len(str(self._sequence)) and int(sequence) <= self._sequence:
                after = int(sequence)
        return (
            f"{self._epoch}:{self._sequence}",
            [touch for number, touch in self._touches.values() if number > after],
        )
