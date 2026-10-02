import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from app.config.settings import Settings
from app.protocol.messages import DeviceState
from app.session.orchestrator import (
    DEVICE_OUTPUT_AUDIO_FRAME_BYTES,
    RelaySession,
    enqueue_latest_audio,
    session_connected_message,
)
from app.tools.web_search import WebSearchResult


class FakeWebSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, object] | bytes] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.events.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        self.events.append(payload)


def test_audio_ingress_queue_keeps_the_most_recent_frame() -> None:
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2)

    assert enqueue_latest_audio(queue, b"first")
    assert enqueue_latest_audio(queue, b"second")
    assert not enqueue_latest_audio(queue, b"third")
    assert [queue.get_nowait(), queue.get_nowait()] == [b"second", b"third"]


def test_upstream_reconnect_is_not_reported_as_a_completed_response() -> None:
    assert session_connected_message("stackchan-001", reconnected=False) == {
        "type": "session.ready",
        "device_id": "stackchan-001",
    }
    assert session_connected_message("stackchan-001", reconnected=True) == {
        "type": "session.reconnected",
        "device_id": "stackchan-001",
    }


class FakeConnection:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []
        self.followups = 0
        self.cancelled = 0
        self.cancel_event_ids: list[str] = []
        self.cancellation_started = asyncio.Event()
        self.truncations: list[dict[str, object]] = []
        self.session_updates: list[dict[str, object]] = []
        self.conversation = SimpleNamespace(
            item=SimpleNamespace(create=self.create_item, truncate=self.truncate_item)
        )
        self.response = SimpleNamespace(create=self.create_followup, cancel=self.cancel_response)
        self.session = SimpleNamespace(update=self.update_session)

    async def update_session(self, *, session: dict[str, object]) -> None:
        self.session_updates.append(session)

    async def create_item(self, *, item: dict[str, object]) -> None:
        self.items.append(item)

    async def create_followup(self) -> None:
        self.followups += 1

    async def cancel_response(self, *, event_id: str) -> None:
        self.cancelled += 1
        self.cancel_event_ids.append(event_id)
        self.cancellation_started.set()

    async def truncate_item(
        self, *, item_id: str, content_index: int, audio_end_ms: int
    ) -> None:
        self.truncations.append(
            {
                "item_id": item_id,
                "content_index": content_index,
                "audio_end_ms": audio_end_ms,
            }
        )


class BlockingSearch:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def search(self, _: str) -> WebSearchResult:
        self.started.set()
        await self.release.wait()
        return WebSearchResult(answer="result", sources=[])


class TimedOutSearch:
    async def search(self, _: str) -> WebSearchResult:
        raise TimeoutError("request timed out")


def test_web_search_function_call_runs_without_blocking_realtime_event_loop() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        search = BlockingSearch()
        session.web_search = search
        connection = FakeConnection()
        event = SimpleNamespace(
            name="search_web", call_id="call-1", arguments='{"query":"latest AI"}'
        )

        session._start_function_call(connection, event)

        await search.started.wait()
        assert session._pending_tool_calls == 1
        assert connection.items == []

        search.release.set()
        await asyncio.gather(*tuple(session._tool_tasks))

        assert session._pending_tool_calls == 0
        assert connection.items[0]["type"] == "function_call_output"

    asyncio.run(scenario())


def test_web_search_timeout_returns_a_tool_error() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        session.web_search = TimedOutSearch()
        connection = FakeConnection()
        event = SimpleNamespace(name="search_web", call_id="call-1", arguments='{"query":"news"}')

        session._start_function_call(connection, event)
        await asyncio.gather(*tuple(session._tool_tasks))

        assert connection.items[0]["output"]
        assert session._pending_tool_calls == 0

    asyncio.run(scenario())


def test_response_done_after_tool_completion_requests_followup() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()
        session._function_call_seen = True

        await session._request_tool_followup(connection)

        assert connection.followups == 1
        assert websocket.events[-1] == {"type": "state", "state": "thinking"}

    asyncio.run(scenario())


def test_pause_cancels_and_truncates_to_the_device_playback_position() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()

        await session._interrupt_response(
            connection,
            {
                "type": "conversation.pause",
                "item_id": "item-1",
                "content_index": 0,
                "audio_end_ms": 1480,
            },
        )

        assert connection.cancelled == 1
        assert connection.truncations == [
            {"item_id": "item-1", "content_index": 0, "audio_end_ms": 1480}
        ]
        assert websocket.events[-1] == {"type": "state", "state": "ready"}

    asyncio.run(scenario())


def test_pause_rejects_invalid_playback_position() -> None:
    assert (
        RelaySession._playback_position(
            {"item_id": "item", "content_index": True, "audio_end_ms": 1}
        )
        is None
    )


def test_late_cancel_error_after_response_done_is_not_sent_to_device() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())

        async def events() -> object:
            yield SimpleNamespace(type="response.done")
            yield SimpleNamespace(
                type="error",
                error=SimpleNamespace(message="Cancellation failed: no active response found"),
            )

        await session._realtime_to_device(events())

        assert not session._cancellation_pending
        assert websocket.events == [
            {"type": "response.done"},
            {"type": "state", "state": "ready"},
        ]

    asyncio.run(scenario())


def test_unrelated_realtime_error_is_still_sent_to_device() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())

        async def events() -> object:
            yield SimpleNamespace(
                type="error",
                error=SimpleNamespace(message="Unexpected upstream failure"),
            )

        await session._realtime_to_device(events())

        assert websocket.events == [
            {
                "type": "error",
                "code": "REALTIME_ERROR",
                "message": "Unexpected upstream failure",
            }
        ]

    asyncio.run(scenario())


def test_reconnect_clears_cancel_state_from_the_previous_foundry_socket() -> None:
    session = RelaySession(FakeWebSocket(), "stackchan-001", Settings())
    session._output_suppressed = True
    session._cancellation_pending = True
    session._announced_output_item = ("old-item", 0)

    session._reset_upstream_response_state()

    assert not session._output_suppressed
    assert not session._cancellation_pending
    assert session._announced_output_item is None


def test_ui_mode_updates_realtime_instructions() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()

        await session._handle_device_control(
            connection, '{"type":"ui.mode","mode":"face"}'
        )

        assert session._face_mode
        assert "にゃん" in connection.session_updates[-1]["instructions"]

        await session._handle_device_control(
            connection, '{"type":"ui.mode","mode":"standard"}'
        )

        assert not session._face_mode
        assert "にゃん" not in connection.session_updates[-1]["instructions"]

    asyncio.run(scenario())


class BlockingOutputWebSocket(FakeWebSocket):
    """Yield immediately before/after one chosen downstream send completes."""

    def __init__(self, send_number: int, *, after_send: bool) -> None:
        super().__init__()
        self.send_number = send_number
        self.after_send = after_send
        self.send_count = 0
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def _send(self, payload: dict[str, object] | bytes) -> None:
        self.send_count += 1
        block = self.send_count == self.send_number
        if self.after_send:
            self.events.append(payload)
        if block:
            self.blocked.set()
            await self.release.wait()
        if not self.after_send:
            self.events.append(payload)

    async def send_json(self, payload: dict[str, object]) -> None:
        await self._send(payload)

    async def send_bytes(self, payload: bytes) -> None:
        await self._send(payload)


def audio_event(
    audio: bytes = b"new audio",
    *,
    event_type: str = "response.output_audio.delta",
    response_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        type=event_type,
        item_id="item-1",
        content_index=0,
        delta=base64.b64encode(audio).decode("ascii"),
        response_id=response_id,
    )


async def send_realtime_events(session: RelaySession, *events: SimpleNamespace) -> None:
    async def stream():
        for event in events:
            yield event

    await session._realtime_to_device(stream())


@pytest.mark.parametrize("event_type", ["response.output_audio.delta", "response.audio.delta"])
@pytest.mark.parametrize("after_send", [False, True])
@pytest.mark.parametrize("send_number", [1, 2, 3, 4])
def test_pause_fence_drains_in_flight_output_and_discards_remaining_pcm(
    event_type: str, after_send: bool, send_number: int
) -> None:
    async def scenario() -> None:
        # Sends are SPEAKING, item metadata, and three PCM frames. Exercise
        # awaits before and after each early send records bytes on the wire.
        websocket = BlockingOutputWebSocket(send_number, after_send=after_send)
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()
        audio = b"x" * (DEVICE_OUTPUT_AUDIO_FRAME_BYTES * 2 + 4)
        output = asyncio.create_task(
            send_realtime_events(session, audio_event(audio, event_type=event_type))
        )
        await websocket.blocked.wait()
        pause = asyncio.create_task(
            session._interrupt_response(
                connection, {"type": "conversation.pause", "interrupt_id": 31}
            )
        )
        await connection.cancellation_started.wait()
        assert not pause.done()
        assert {"type": "conversation.paused", "interrupt_id": 31} not in websocket.events
        websocket.release.set()
        await asyncio.wait_for(asyncio.gather(output, pause), timeout=1)

        assert websocket.events[-2:] == [
            {"type": "state", "state": "ready"},
            {"type": "conversation.paused", "interrupt_id": 31},
        ]
        assert len([event for event in websocket.events if isinstance(event, bytes)]) == max(
            0, send_number - 2
        )
        # Even another complete upstream delta cannot cross the fence.
        await send_realtime_events(session, audio_event(b"stale"))
        assert websocket.events[-1] == {"type": "conversation.paused", "interrupt_id": 31}

    asyncio.run(scenario())


def test_output_waiting_for_downstream_lock_rechecks_cancellation() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()
        entered = asyncio.Event()

        async def output() -> None:
            generation = session._output_generation
            entered.set()
            await session._send_response_audio(audio_event(b"stale"), generation)

        await session._downstream_send_lock.acquire()
        pending_output = asyncio.create_task(output())
        await entered.wait()
        pause = asyncio.create_task(
            session._interrupt_response(
                connection, {"type": "conversation.pause", "interrupt_id": 32}
            )
        )
        await connection.cancellation_started.wait()
        session._downstream_send_lock.release()
        await asyncio.wait_for(asyncio.gather(pending_output, pause), timeout=1)
        assert websocket.events == [
            {"type": "state", "state": "ready"},
            {"type": "conversation.paused", "interrupt_id": 32},
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt_id", [None, True, False, 0, -1, "1", 1.5, 0x100000000])
def test_invalid_or_missing_interrupt_id_does_not_emit_a_fence(interrupt_id: object) -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()
        await session._interrupt_response(
            connection, {"type": "conversation.pause", "interrupt_id": interrupt_id}
        )
        assert connection.cancelled == 1
        assert websocket.events == [{"type": "state", "state": "ready"}]

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_end", ["done", "error"])
def test_repeated_pause_preserves_listening_and_allows_the_next_turn(cancel_end: str) -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()
        for interrupt_id in range(1, 4):
            await session._interrupt_response(
                connection, {"type": "conversation.pause", "interrupt_id": interrupt_id}
            )
            await session._handle_device_control(connection, '{"type":"audio.start"}')
            next_turn_start = len(websocket.events)
            await send_realtime_events(
                session,
                SimpleNamespace(type="response.created"),
                audio_event(b"stale"),
                SimpleNamespace(
                    type="response.function_call_arguments.done",
                    name="set_emotion",
                    call_id="stale-call",
                    arguments='{"emotion":"angry"}',
                ),
                (
                    SimpleNamespace(type="response.done")
                    if cancel_end == "done"
                    else SimpleNamespace(
                        type="error",
                        error=SimpleNamespace(
                            message="Cancellation failed: no active response found",
                            event_id=connection.cancel_event_ids[-1],
                        ),
                    )
                ),
            )
            assert websocket.events[next_turn_start:] == []
            assert session._state == DeviceState.LISTENING
            assert not session._tool_tasks
            assert connection.followups == 0
            assert connection.items == []
            await send_realtime_events(
                session,
                SimpleNamespace(type="response.created"),
                audio_event(b"new turn"),
                SimpleNamespace(type="response.done"),
            )
            assert websocket.events[next_turn_start:] == [
                {"type": "state", "state": "thinking"},
                {"type": "state", "state": "speaking"},
                {"type": "output_audio.started", "item_id": "item-1", "content_index": 0},
                b"new turn",
                {"type": "response.done"},
                {"type": "state", "state": "ready"},
            ]
        assert [
            event["interrupt_id"]
            for event in websocket.events
            if isinstance(event, dict) and event.get("type") == "conversation.paused"
        ] == [1, 2, 3]

    asyncio.run(scenario())


def test_response_done_paused_during_send_does_not_restore_ready_over_listening() -> None:
    async def scenario() -> None:
        websocket = BlockingOutputWebSocket(1, after_send=False)
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()
        output = asyncio.create_task(
            send_realtime_events(session, SimpleNamespace(type="response.done"))
        )
        await websocket.blocked.wait()
        pause = asyncio.create_task(
            session._interrupt_response(
                connection, {"type": "conversation.pause", "interrupt_id": 33}
            )
        )
        await connection.cancellation_started.wait()
        websocket.release.set()
        await asyncio.wait_for(asyncio.gather(output, pause), timeout=1)
        await session._handle_device_control(connection, '{"type":"audio.start"}')
        await send_realtime_events(session, SimpleNamespace(type="response.done"))
        assert websocket.events == [
            {"type": "response.done"},
            {"type": "state", "state": "ready"},
            {"type": "conversation.paused", "interrupt_id": 33},
            {"type": "state", "state": "listening"},
        ]
        assert session._state == DeviceState.LISTENING

    asyncio.run(scenario())


def test_pause_cancels_tool_waiting_for_downstream_lock_without_deadlock() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()
        await session._downstream_send_lock.acquire()
        session._start_function_call(
            connection,
            SimpleNamespace(name="search_web", call_id="call-1", arguments='{"query":"news"}'),
        )
        pause = asyncio.create_task(
            session._interrupt_response(
                connection, {"type": "conversation.pause", "interrupt_id": 34}
            )
        )
        await asyncio.wait_for(connection.cancellation_started.wait(), timeout=1)
        session._downstream_send_lock.release()
        await asyncio.wait_for(pause, timeout=1)
        assert not session._tool_tasks
        assert session._pending_tool_calls == 0
        assert connection.followups == 0
        assert websocket.events[-1] == {"type": "conversation.paused", "interrupt_id": 34}

    asyncio.run(scenario())


def test_tool_returning_during_cancellation_cannot_send_output_or_request_followup() -> None:
    class CancellationResistantSearch(BlockingSearch):
        async def search(self, query: str) -> WebSearchResult:
            try:
                return await super().search(query)
            except asyncio.CancelledError:
                return WebSearchResult(answer="stale result", sources=[])

    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()
        search = CancellationResistantSearch()
        session.web_search = search
        session._start_function_call(
            connection,
            SimpleNamespace(name="search_web", call_id="call-1", arguments='{"query":"news"}'),
        )
        await search.started.wait()
        session._response_done_waiting_for_tools = True
        await session._interrupt_response(
            connection, {"type": "conversation.pause", "interrupt_id": 35}
        )
        assert connection.items == []
        assert connection.followups == 0
        assert not session._tool_tasks
        assert session._pending_tool_calls == 0
        assert websocket.events[-1] == {"type": "conversation.paused", "interrupt_id": 35}

    asyncio.run(scenario())


def test_pause_invalidates_tool_followup_waiting_for_upstream_lock() -> None:
    async def scenario() -> None:
        websocket = BlockingOutputWebSocket(1, after_send=True)
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()
        await session._upstream_send_lock.acquire()
        followup = asyncio.create_task(session._request_tool_followup(connection))
        await websocket.blocked.wait()
        pause = asyncio.create_task(
            session._interrupt_response(
                connection, {"type": "conversation.pause", "interrupt_id": 36}
            )
        )
        # Let pause enter before releasing either send. The explicit task yield
        # is local scheduling, rather than a timing-dependent network sleep.
        await asyncio.sleep(0)
        websocket.release.set()
        session._upstream_send_lock.release()
        await asyncio.wait_for(asyncio.gather(followup, pause), timeout=1)
        assert connection.followups == 0
        assert websocket.events[-1] == {"type": "conversation.paused", "interrupt_id": 36}

    asyncio.run(scenario())


def test_upstream_reconnect_cancelling_pause_still_emits_fence_and_new_audio() -> None:
    class DisconnectingConnection(FakeConnection):
        async def cancel_response(self, *, event_id: str) -> None:
            await super().cancel_response(event_id=event_id)
            await asyncio.Event().wait()

    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = DisconnectingConnection()
        pause = asyncio.create_task(
            session._interrupt_response(
                connection, {"type": "conversation.pause", "interrupt_id": 37}
            )
        )
        await connection.cancellation_started.wait()
        pause.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pause
        assert websocket.events[-1] == {"type": "conversation.paused", "interrupt_id": 37}
        session._reset_upstream_response_state()
        await send_realtime_events(
            session, SimpleNamespace(type="response.created"), audio_event(b"reconnected")
        )
        assert websocket.events[-1] == b"reconnected"

    asyncio.run(scenario())


@pytest.mark.parametrize("message_type", ["conversation.pause", "response.cancel"])
@pytest.mark.parametrize("interrupt_id", [1, 0xFFFFFFFF])
def test_both_interrupt_controls_echo_valid_uint32_fences(
    message_type: str, interrupt_id: int
) -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        await session._interrupt_response(
            FakeConnection(), {"type": message_type, "interrupt_id": interrupt_id}
        )
        assert websocket.events[-1] == {
            "type": "conversation.paused", "interrupt_id": interrupt_id
        }

    asyncio.run(scenario())


def response_event(event_type: str, response_id: str) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, response=SimpleNamespace(id=response_id))


def cancel_error(event_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="error",
        error=SimpleNamespace(
            message="Cancellation failed: no active response found", event_id=event_id
        ),
    )


def test_old_response_ids_and_cancel_errors_cannot_revive_output_after_a_new_turn() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()
        await send_realtime_events(session, response_event("response.created", "old-1"))
        await session._interrupt_response(
            connection, {"type": "conversation.pause", "interrupt_id": 41}
        )
        first_cancel_event_id = connection.cancel_event_ids[-1]
        await send_realtime_events(session, cancel_error(first_cancel_event_id))
        await session._handle_device_control(connection, '{"type":"audio.start"}')
        await send_realtime_events(session, response_event("response.created", "new-2"))
        fresh_response_start = len(websocket.events)
        await send_realtime_events(
            session,
            cancel_error(first_cancel_event_id),
            SimpleNamespace(
                type="error",
                error=SimpleNamespace(message="Cancellation failed: no active response found"),
            ),
            response_event("response.created", "old-1"),
            audio_event(b"stale old response", response_id="old-1"),
            SimpleNamespace(
                type="response.function_call_arguments.done",
                response_id="old-1",
                name="set_emotion",
                call_id="old-call",
                arguments='{"emotion":"angry"}',
            ),
            response_event("response.done", "old-1"),
        )
        assert websocket.events[fresh_response_start:] == []
        assert session._active_response_id == "new-2"
        assert session._state == DeviceState.THINKING
        assert not session._tool_tasks
        await send_realtime_events(session, audio_event(b"fresh", response_id="new-2"))
        assert websocket.events[-1] == b"fresh"

        # The first cancel's delayed error/done cannot clear the second pause.
        await session._interrupt_response(
            connection, {"type": "conversation.pause", "interrupt_id": 42}
        )
        second_cancel_event_id = connection.cancel_event_ids[-1]
        second_pause_end = len(websocket.events)
        await send_realtime_events(
            session,
            cancel_error(first_cancel_event_id),
            response_event("response.done", "old-1"),
            response_event("response.created", "new-2"),
            audio_event(b"stale second response", response_id="new-2"),
        )
        assert session._cancellation_pending
        assert websocket.events[second_pause_end:] == []
        await send_realtime_events(session, cancel_error(second_cancel_event_id))
        await session._handle_device_control(connection, '{"type":"audio.start"}')
        await send_realtime_events(
            session,
            response_event("response.created", "new-3"),
            audio_event(b"third turn", response_id="new-3"),
            response_event("response.done", "new-3"),
        )
        assert b"third turn" in websocket.events
        assert websocket.events[-1] == {"type": "state", "state": "ready"}

    asyncio.run(scenario())


def test_done_from_retired_response_cannot_release_cancel_before_response_created() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        connection = FakeConnection()
        await send_realtime_events(
            session,
            response_event("response.created", "completed"),
            response_event("response.done", "completed"),
        )
        await session._interrupt_response(
            connection, {"type": "conversation.pause", "interrupt_id": 43}
        )
        fence_end = len(websocket.events)
        await send_realtime_events(
            session,
            response_event("response.done", "completed"),
            response_event("response.created", "cancelled-before-created"),
            audio_event(b"stale", response_id="cancelled-before-created"),
        )
        assert session._cancellation_pending
        assert websocket.events[fence_end:] == []
        await session._handle_device_control(connection, '{"type":"audio.start"}')
        await send_realtime_events(
            session,
            response_event("response.done", "cancelled-before-created"),
            response_event("response.created", "new"),
            audio_event(b"next turn", response_id="new"),
        )
        assert not session._cancellation_pending
        assert websocket.events[-1] == b"next turn"

    asyncio.run(scenario())


@pytest.mark.parametrize("event_id", [None, ""])
def test_uncorrelated_cancel_error_reconnects_after_fence_and_recovers_next_turn(
    monkeypatch: pytest.MonkeyPatch, event_id: str | None
) -> None:
    class SessionWebSocket(FakeWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            self.outgoing: asyncio.Queue[dict[str, object] | bytes] = asyncio.Queue()

        async def receive(self) -> dict[str, object]:
            return await self.incoming.get()

        async def send_json(self, payload: dict[str, object]) -> None:
            await super().send_json(payload)
            self.outgoing.put_nowait(payload)

        async def send_bytes(self, payload: bytes) -> None:
            await super().send_bytes(payload)
            self.outgoing.put_nowait(payload)

        async def wait_for_output(self, expected: dict[str, object] | bytes) -> None:
            while await self.outgoing.get() != expected:
                pass

    class SessionConnection(FakeConnection):
        def __init__(self, *, block_cancel: bool = False) -> None:
            super().__init__()
            self.block_cancel = block_cancel
            self.events: asyncio.Queue[SimpleNamespace] = asyncio.Queue()
            self.input_audio_buffer = SimpleNamespace(append=self.append_audio)
            self.input_audio: list[bytes] = []
            self.audio_appended = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self) -> SimpleNamespace:
            return await self.events.get()

        async def append_audio(self, *, audio: str) -> None:
            self.input_audio.append(base64.b64decode(audio))
            self.audio_appended.set()

        async def cancel_response(self, *, event_id: str) -> None:
            await super().cancel_response(event_id=event_id)
            if self.block_cancel:
                await asyncio.Event().wait()

    class SessionRealtime:
        def __init__(self, connections: list[SessionConnection]) -> None:
            self.connections = connections
            self.connected = 0
            self.closed = 0

        @asynccontextmanager
        async def connect(self, *, face_mode: bool):
            connection = self.connections[self.connected]
            self.connected += 1
            try:
                yield connection
            finally:
                self.closed += 1

    monkeypatch.setattr("app.session.orchestrator.UPSTREAM_RECONNECT_DELAY_SECONDS", 0)

    async def scenario() -> None:
        websocket = SessionWebSocket()
        session = RelaySession(websocket, "stackchan-001", Settings())
        old_connection = SessionConnection(block_cancel=True)
        new_connection = SessionConnection()
        realtime = SessionRealtime([old_connection, new_connection])
        session.realtime = realtime
        running = asyncio.create_task(session.run())
        try:
            await websocket.wait_for_output({"type": "state", "state": "ready"})
            websocket.incoming.put_nowait(
                {
                    "text": '{"type":"hello","device_id":"stackchan-001","audio":{}}'
                }
            )
            await websocket.wait_for_output({"type": "hello.ack", "protocol": 1})
            old_connection.events.put_nowait(response_event("response.created", "old"))
            await websocket.wait_for_output({"type": "state", "state": "thinking"})
            websocket.incoming.put_nowait(
                {"text": '{"type":"conversation.pause","interrupt_id":51}'}
            )
            await old_connection.cancellation_started.wait()
            # Force real _run_connection teardown while pause is awaiting the
            # upstream cancel send. Its finally must still send the fence.
            old_connection.events.put_nowait(
                SimpleNamespace(
                    type="error",
                    error=SimpleNamespace(
                        message="Cancellation failed: no active response found",
                        event_id=event_id,
                    ),
                )
            )
            old_connection.events.put_nowait(response_event("response.created", "stale"))
            old_connection.events.put_nowait(audio_event(b"stale", response_id="stale"))
            await websocket.wait_for_output(
                {"type": "session.reconnected", "device_id": "stackchan-001"}
            )
            await websocket.wait_for_output({"type": "state", "state": "ready"})
            assert realtime.connected == 2
            assert realtime.closed == 1
            fence = {"type": "conversation.paused", "interrupt_id": 51}
            assert websocket.events.count(fence) == 1
            assert websocket.events.index(fence) < websocket.events.index(
                {"type": "session.reconnected", "device_id": "stackchan-001"}
            )
            assert not session._cancellation_pending
            assert not session._output_suppressed
            assert not session._retired_response_ids
            assert session._pending_cancel_event_id is None
            assert session._pending_cancel_response_id is None
            assert not session._tool_tasks
            assert b"stale" not in websocket.events

            websocket.incoming.put_nowait({"text": '{"type":"audio.start"}'})
            await websocket.wait_for_output({"type": "state", "state": "listening"})
            websocket.incoming.put_nowait({"bytes": b"next microphone frame"})
            await new_connection.audio_appended.wait()
            assert new_connection.input_audio == [b"next microphone frame"]
            new_connection.events.put_nowait(response_event("response.created", "new"))
            new_connection.events.put_nowait(audio_event(b"next turn", response_id="new"))
            new_connection.events.put_nowait(response_event("response.done", "new"))
            await websocket.wait_for_output(b"next turn")
            await websocket.wait_for_output({"type": "response.done"})
            await websocket.wait_for_output({"type": "state", "state": "ready"})
            websocket.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            with pytest.raises(WebSocketDisconnect):
                await running
            assert realtime.closed == 2
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(asyncio.wait_for(scenario(), timeout=2))
