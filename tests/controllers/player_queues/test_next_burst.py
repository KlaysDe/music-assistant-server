"""Exercise adaptive Next timing and prevent stale queued steps from starting playback."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _controller() -> PlayerQueuesController:
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.mass = MagicMock()
    controller.mass.players.get_player.return_value = None
    controller.mass.get_task.return_value = None
    controller.logger = Mock()
    controller._queue_data = {
        queue_id: PlayerQueueData(
            queue=PlayerQueue(
                queue_id=queue_id,
                display_name=queue_id,
                available=True,
                items=10,
                active=True,
                current_index=0,
                state=PlaybackState.PLAYING,
            ),
        )
        for queue_id in ("main", "other")
    }
    controller._check_player_permission = Mock()  # type: ignore[method-assign]
    controller._get_next_index = Mock(side_effect=lambda _queue, index, _skip: index + 1)  # type: ignore[method-assign]
    controller.get_item = Mock()  # type: ignore[method-assign]
    controller.signal_update = Mock()  # type: ignore[method-assign]
    return controller


async def _press(
    controller: PlayerQueuesController, queue_id: str, timestamp: float, *, held_for: float = 0
) -> float:
    controller.mass.loop.time.side_effect = [timestamp, timestamp + held_for]
    await controller.next(queue_id)
    timer = controller.mass.call_later.call_args
    assert timer.args[2] == queue_id
    assert timer.kwargs["task_id"] == f"queue_play_index_{queue_id}"
    return timer.args[0]


async def test_only_third_and_later_rapid_presses_wait() -> None:
    """Keep the first two responsive and schedule only the final target in a burst."""
    controller = _controller()
    timers = {}
    controller.mass.call_later.side_effect = lambda *args, task_id, **kwargs: timers.update(
        {task_id: (args, kwargs)}
    )

    assert await _press(controller, "main", 10.0) == 0
    assert await _press(controller, "main", 10.1) == 0
    assert await _press(controller, "main", 10.2) == pytest.approx(0.35)
    assert await _press(controller, "other", 10.25) == 0
    assert await _press(controller, "main", 10.3) == pytest.approx(0.35)
    assert timers["queue_play_index_main"][0][3] == 4
    assert controller._queue_data["main"].queue.current_index == 4
    assert await _press(controller, "main", 10.7) == 0


async def test_time_waiting_for_playback_lock_counts_toward_debounce() -> None:
    """Use the tap time for both burst detection and the timer deadline."""
    controller = _controller()
    await _press(controller, "main", 10.0)
    await _press(controller, "main", 10.1, held_for=0.2)
    assert await _press(controller, "main", 10.2, held_for=0.25) == pytest.approx(0.1)
    assert await _press(controller, "main", 10.3, held_for=0.5) == 0


async def test_superseded_next_cannot_start_playback() -> None:
    """Check the pending request after the callback obtains the playback lock."""
    controller = _controller()
    controller._play_index = AsyncMock()  # type: ignore[method-assign]
    await _press(controller, "main", 10.0)
    await _press(controller, "main", 10.1)

    # The timer registers the callback as the current queue step.
    controller.mass.get_task.side_effect = lambda _task_id: asyncio.current_task()
    await controller._play_next_index("main", 1, 10.0)
    controller._play_index.assert_not_awaited()
    await controller._play_next_index("main", 2, 10.1)
    controller._play_index.assert_awaited_once_with("main", 2, None, False)


async def test_explicit_playback_cancellation_resets_next_burst() -> None:
    """An explicit new playback action clears pending steps and their streak."""
    controller = _controller()
    await _press(controller, "main", 10.0)
    await _press(controller, "main", 10.1)
    assert await _press(controller, "main", 10.2) == pytest.approx(0.35)
    controller._drop_pending_step("main")
    controller.mass.cancel_timer.assert_called_with("queue_play_index_main")
    assert await _press(controller, "main", 10.3) == 0
