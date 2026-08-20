import asyncio
from types import SimpleNamespace

from app.config.settings import Settings
from app.session.orchestrator import RelaySession, enqueue_latest_audio, session_connected_message
from app.tools.web_search import WebSearchResult


class FakeWebSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
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

    async def cancel_response(self) -> None:
        self.cancelled += 1

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
