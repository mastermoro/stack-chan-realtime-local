import asyncio
import base64
import json
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from app.config.settings import Settings
from app.protocol.messages import DeviceState, HelloMessage, SourcesMessage
from app.realtime.client import FoundryRealtimeClient
from app.tools.local_actions import LocalActionExecutor
from app.tools.web_search import WebSearchExecutor

logger = logging.getLogger(__name__)
UPSTREAM_RECONNECT_DELAY_SECONDS = 1
# Stack-chan sends 20 ms audio frames. Limit ingress lag to 100 ms.
DEVICE_AUDIO_QUEUE_FRAMES = 5
# ESP32 WebSockets limits individual incoming frames to 15 KiB. Keep relay
# frames comfortably below that limit while preserving PCM byte order.
DEVICE_OUTPUT_AUDIO_FRAME_BYTES = 8 * 1024


class UpstreamResetRequired(Exception):
    """The current upstream session cannot safely continue producing output."""


def enqueue_latest_audio(queue: asyncio.Queue[bytes], chunk: bytes) -> bool:
    """Put a live audio frame into a bounded queue, evicting stale audio first."""
    try:
        queue.put_nowait(chunk)
    except asyncio.QueueFull:
        queue.get_nowait()
        queue.put_nowait(chunk)
        return False
    return True


def session_connected_message(device_id: str, *, reconnected: bool) -> dict[str, object]:
    """Describe a new upstream session without faking a completed response."""
    if reconnected:
        return {"type": "session.reconnected", "device_id": device_id}
    return {"type": "session.ready", "device_id": device_id}


class RelaySession:
    def __init__(self, websocket: WebSocket, device_id: str, settings: Settings) -> None:
        self.websocket = websocket
        self.device_id = device_id
        self.settings = settings
        self.local_actions = LocalActionExecutor(settings)
        self.realtime = FoundryRealtimeClient(
            settings,
            additional_tools=self.local_actions.tool_definitions(),
        )
        self.web_search = WebSearchExecutor(settings)
        self._hello_received = False
        self._state = DeviceState.READY
        self._upstream_send_lock = asyncio.Lock()
        self._downstream_send_lock = asyncio.Lock()
        self._tool_tasks: set[asyncio.Task[None]] = set()
        self._pending_tool_calls = 0
        self._function_call_seen = False
        self._response_done_waiting_for_tools = False
        self._device_audio_queue: asyncio.Queue[bytes] | None = None
        self._dropped_audio_frames = 0
        self._output_suppressed = False
        self._cancellation_pending = False
        self._upstream_reset_required = False
        self._output_generation = 0
        self._active_response_id: str | None = None
        self._retired_response_ids: set[str] = set()
        self._pending_cancel_event_id: str | None = None
        self._pending_cancel_response_id: str | None = None
        self._announced_output_item: tuple[str, int] | None = None
        self._face_mode = False

    async def run(self) -> None:
        connected_once = False
        while True:
            try:
                async with self.realtime.connect(face_mode=self._face_mode) as connection:
                    await self.websocket.send_json(
                        session_connected_message(self.device_id, reconnected=connected_once)
                    )
                    await self._set_state(DeviceState.READY)
                    connected_once = True
                    await self._run_connection(connection)
                    logger.warning("Foundry session ended device=%s; reconnecting", self.device_id)
                    await self.websocket.send_json(
                        {
                            "type": "error",
                            "code": "UPSTREAM_RECONNECTING",
                            "message": "Foundry session ended; reconnecting.",
                        }
                    )
                    await asyncio.sleep(UPSTREAM_RECONNECT_DELAY_SECONDS)
            except WebSocketDisconnect:
                raise
            except (ConnectionClosed, OSError, UpstreamResetRequired) as exc:
                logger.warning("Foundry session disconnected device=%s: %s", self.device_id, exc)
                await self.websocket.send_json(
                    {
                        "type": "error",
                        "code": "UPSTREAM_RECONNECTING",
                        "message": "Foundry connection was reset; reconnecting.",
                    }
                )
                await asyncio.sleep(UPSTREAM_RECONNECT_DELAY_SECONDS)

    async def _run_connection(self, connection: Any) -> None:
        # A reconnected Foundry socket is a new Realtime session. Never carry a
        # cancel/suppression marker from the previous socket into it, otherwise
        # every response for the next user turn is mistaken for stale audio.
        self._reset_upstream_response_state()
        self._device_audio_queue = asyncio.Queue(maxsize=DEVICE_AUDIO_QUEUE_FRAMES)
        self._dropped_audio_frames = 0
        device_task = asyncio.create_task(
            self._device_to_realtime(connection), name="device_to_realtime"
        )
        audio_sender_task = asyncio.create_task(
            self._send_queued_audio(connection), name="send_queued_audio"
        )
        realtime_task = asyncio.create_task(
            self._realtime_to_device(connection), name="realtime_to_device"
        )
        tasks = {device_task, audio_sender_task, realtime_task}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            logger.warning(
                "connection task completed device=%s tasks=%s",
                self.device_id,
                ",".join(task.get_name() for task in done),
            )
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc
            if device_task in done:
                raise WebSocketDisconnect(code=1000)
        finally:
            # Join all senders even if this supervisor itself is cancelled or a
            # child raises. In-progress interrupts finish their output fence
            # before a replacement upstream session can reset suppression.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._clear_queued_audio()
            self._device_audio_queue = None
            await self._cancel_tool_tasks()

    def _reset_upstream_response_state(self) -> None:
        self._output_generation += 1
        self._output_suppressed = False
        self._cancellation_pending = False
        self._upstream_reset_required = False
        self._announced_output_item = None
        self._active_response_id = None
        self._retired_response_ids.clear()
        self._pending_cancel_event_id = None
        self._pending_cancel_response_id = None

    async def _cancel_tool_tasks(self) -> None:
        tasks = tuple(self._tool_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tool_tasks.clear()
        self._pending_tool_calls = 0
        self._function_call_seen = False
        self._response_done_waiting_for_tools = False

    def _clear_queued_audio(self) -> None:
        if self._device_audio_queue is None:
            return
        while not self._device_audio_queue.empty():
            self._device_audio_queue.get_nowait()

    def _queue_device_audio(self, audio: bytes) -> None:
        if self._device_audio_queue is None:
            return
        if not enqueue_latest_audio(self._device_audio_queue, audio):
            self._dropped_audio_frames += 1
            if self._dropped_audio_frames == 1 or self._dropped_audio_frames % 50 == 0:
                logger.warning(
                    "Dropping stale device audio device=%s frames=%s",
                    self.device_id,
                    self._dropped_audio_frames,
                )

    async def _device_to_realtime(self, connection: Any) -> None:
        while True:
            message = await self.websocket.receive()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(code=message.get("code", 1000))

            audio = message.get("bytes")
            if audio is not None:
                if not self._hello_received:
                    await self._send_error("PROTOCOL_ERROR", "hello message is required first")
                    continue
                if audio:
                    if self._state != DeviceState.LISTENING:
                        logger.debug(
                            "Ignoring audio outside listening state device=%s", self.device_id
                        )
                        continue
                    self._queue_device_audio(audio)
                continue

            text = message.get("text")
            if text is None:
                continue
            await self._handle_device_control(connection, text)

    async def _send_queued_audio(self, connection: Any) -> None:
        if self._device_audio_queue is None:
            return
        while True:
            audio = await self._device_audio_queue.get()
            # State can change while the frame was queued or waiting for the
            # serialized upstream sender. Do not send stale prior-turn audio.
            if self._state != DeviceState.LISTENING:
                continue
            async with self._upstream_send_lock:
                if self._state != DeviceState.LISTENING:
                    continue
                await connection.input_audio_buffer.append(
                    audio=base64.b64encode(audio).decode("ascii")
                )

    async def _handle_device_control(self, connection: Any, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await self._send_error("INVALID_JSON", "control frame must be JSON")
            return

        message_type = payload.get("type")
        if message_type == "hello":
            hello = HelloMessage.model_validate(payload)
            if hello.device_id != self.device_id:
                await self._send_error("DEVICE_ID_MISMATCH", "device ID differs from auth header")
                return
            self._hello_received = True
            await self.websocket.send_json({"type": "hello.ack", "protocol": 1})
        elif message_type == "audio.start":
            await self._set_state(DeviceState.LISTENING)
        elif message_type == "audio.stop":
            # Server VAD is authoritative; this is retained for UI and future manual VAD.
            await self._set_state(DeviceState.THINKING)
        elif message_type in {"response.cancel", "conversation.pause"}:
            await self._interrupt_response(connection, payload)
        elif message_type == "ui.mode":
            mode = payload.get("mode")
            if mode not in {"standard", "face"}:
                await self._send_error("INVALID_UI_MODE", "mode must be standard or face")
                return
            self._face_mode = mode == "face"
            await connection.session.update(
                session={
                    "type": "realtime",
                    "instructions": self.realtime.instructions(face_mode=self._face_mode),
                }
            )
        elif message_type == "ping":
            await self.websocket.send_json({"type": "pong"})
        else:
            await self._send_error("UNKNOWN_CONTROL", f"unsupported control type: {message_type}")

    async def _realtime_to_device(self, connection: Any) -> None:
        async for event in connection:
            event_type = event.type
            generation = self._output_generation
            response_id = self._event_response_id(event)

            if event_type == "response.created":
                # Foundry can still deliver response.created after a client has
                # cancelled it. Keep that stale response muted until response.done.
                async with self._downstream_send_lock:
                    if response_id in self._retired_response_ids:
                        continue
                    if (
                        self._upstream_reset_required
                        or self._cancellation_pending
                        or generation != self._output_generation
                    ):
                        if response_id is not None:
                            self._retired_response_ids.add(response_id)
                            if self._pending_cancel_response_id is None:
                                self._pending_cancel_response_id = response_id
                        continue
                    self._active_response_id = response_id
                    self._output_suppressed = False
                    self._announced_output_item = None
                    await self._set_state_locked(DeviceState.THINKING)
            elif event_type in {"response.output_audio.delta", "response.audio.delta"}:
                if self._response_event_allowed(event):
                    await self._send_response_audio(event, generation)
            elif event_type == "conversation.item.input_audio_transcription.completed":
                if self.settings.transcript_logging:
                    logger.info("device=%s transcript=%s", self.device_id, event.transcript)
                await self.websocket.send_json(
                    {"type": "transcript.user", "text": event.transcript}
                )
            elif event_type == "response.function_call_arguments.done":
                self._start_function_call(connection, event)
            elif event_type == "response.done":
                logger.info("response.done device=%s; retaining device WebSocket", self.device_id)
                # A cancelled response belongs to the old turn. In particular,
                # it must not restore READY after the device starts listening.
                if self._cancellation_pending or self._output_suppressed:
                    if (
                        response_id is None
                        or response_id == self._pending_cancel_response_id
                        or (
                            self._pending_cancel_response_id is None
                            and response_id not in self._retired_response_ids
                        )
                    ):
                        self._cancellation_pending = False
                        self._active_response_id = None
                    if response_id is not None:
                        self._retired_response_ids.add(response_id)
                    continue
                if not self._response_event_allowed(event):
                    continue
                if response_id is not None:
                    self._retired_response_ids.add(response_id)
                self._active_response_id = None
                if self._function_call_seen:
                    if self._pending_tool_calls:
                        self._response_done_waiting_for_tools = True
                    else:
                        await self._request_tool_followup(connection, generation)
                else:
                    async with self._downstream_send_lock:
                        if not self._response_output_allowed(generation):
                            continue
                        await self.websocket.send_json({"type": "response.done"})
                        if self._response_output_allowed(generation):
                            await self._set_state_locked(DeviceState.READY)
            elif event_type == "error":
                message = getattr(getattr(event, "error", None), "message", "Realtime error")
                if "cancellation failed: no active response found" in message.lower():
                    # A late failure for an earlier cancel must not release a
                    # newer pause. Some upstream peers omit the optional
                    # event_id, so a pending cancel cannot safely use that error
                    # as its completion. Reconnect instead of unmuting old audio
                    # or leaving the next turn permanently suppressed.
                    cancel_event_id = getattr(getattr(event, "error", None), "event_id", None)
                    if not isinstance(cancel_event_id, str) or not cancel_event_id:
                        if self._cancellation_pending:
                            self._upstream_reset_required = True
                            logger.warning(
                                "Uncorrelated cancellation error device=%s; reconnecting "
                                "upstream and resetting conversation history",
                                self.device_id,
                            )
                            return
                    elif (
                        self._cancellation_pending
                        and cancel_event_id == self._pending_cancel_event_id
                    ):
                        self._cancellation_pending = False
                        self._active_response_id = None
                    logger.debug("response was already complete when cancelled")
                    continue
                logger.error("Realtime API error device=%s: %s", self.device_id, message)
                await self._send_error("REALTIME_ERROR", message)

    async def _interrupt_response(self, connection: Any, payload: dict[str, Any]) -> None:
        """Cancel output and synchronize server history with device playback."""
        self._output_generation += 1
        self._output_suppressed = True
        if self._active_response_id is not None:
            self._retired_response_ids.add(self._active_response_id)
        self._pending_cancel_response_id = self._active_response_id
        cancel_event_id = f"stackchan-cancel-{self._output_generation}"
        self._pending_cancel_event_id = cancel_event_id
        # Mark cancellation before any await: response.created/done can arrive
        # while response.cancel is still sending on the upstream socket.
        self._cancellation_pending = True
        # Do not hold the downstream lock while joining tools. A cancelled tool
        # can be waiting for that lock (or unwinding a send that holds it).
        try:
            await self._cancel_tool_tasks()
            async with self._upstream_send_lock:
                try:
                    await connection.response.cancel(event_id=cancel_event_id)
                except Exception as exc:
                    # SDK send failures are not Realtime's no-active-response
                    # error (which arrives as an event). Delivery is uncertain:
                    # keep every response muted until a clean session replaces
                    # this one, including when done/error races with teardown.
                    self._upstream_reset_required = True
                    raise UpstreamResetRequired(
                        "Could not send response.cancel; resetting conversation history"
                    ) from exc
                position = self._playback_position(payload)
                if position is not None:
                    item_id, content_index, audio_end_ms = position
                    try:
                        await connection.conversation.item.truncate(
                            item_id=item_id,
                            content_index=content_index,
                            audio_end_ms=audio_end_ms,
                        )
                    except Exception:
                        # A client can pause before Foundry has registered its first
                        # output item. Cancellation is still useful in that case.
                        logger.debug("response truncate ignored", exc_info=True)
        finally:
            # Teardown may cancel this task while it is already sending the
            # fence. Shield that send and join it before propagating cancellation
            # so the replacement session cannot strand a device waiting for ack.
            fence = asyncio.create_task(self._send_interrupt_fence(payload))
            cancelled = False
            while not fence.done():
                try:
                    await asyncio.shield(fence)
                except asyncio.CancelledError:
                    cancelled = True
            fence.result()
            if cancelled:
                raise asyncio.CancelledError

    async def _send_interrupt_fence(self, payload: dict[str, Any]) -> None:
        # Drain any send already in progress. Output rechecks prevent the rest
        # of an old delta from resuming behind this ordered fence.
        async with self._downstream_send_lock:
            await self._set_state_locked(DeviceState.READY)
            interrupt_id = payload.get("interrupt_id")
            if (
                payload.get("type") in {"conversation.pause", "response.cancel"}
                and isinstance(interrupt_id, int)
                and not isinstance(interrupt_id, bool)
                and 0 < interrupt_id <= 0xFFFFFFFF
            ):
                await self.websocket.send_json(
                    {"type": "conversation.paused", "interrupt_id": interrupt_id}
                )

    def _response_output_allowed(self, generation: int) -> bool:
        return (
            generation == self._output_generation
            and not self._upstream_reset_required
            and not self._output_suppressed
            and not self._cancellation_pending
        )

    @staticmethod
    def _event_response_id(event: Any) -> str | None:
        response_id = getattr(event, "response_id", None)
        if response_id is None:
            response_id = getattr(getattr(event, "response", None), "id", None)
        return response_id if isinstance(response_id, str) and response_id else None

    def _response_event_allowed(self, event: Any) -> bool:
        response_id = self._event_response_id(event)
        return response_id is None or (
            response_id not in self._retired_response_ids
            and self._active_response_id in {None, response_id}
        )

    async def _send_response_audio(self, event: Any, generation: int) -> None:
        async with self._downstream_send_lock:
            if (
                not self._response_output_allowed(generation)
                or not self._response_event_allowed(event)
            ):
                return
            await self._set_state_locked(DeviceState.SPEAKING)
            if not self._response_output_allowed(generation):
                return
            item_id = getattr(event, "item_id", None)
            content_index = getattr(event, "content_index", None)
            if isinstance(item_id, str) and isinstance(content_index, int):
                output_item = (item_id, content_index)
                if output_item != self._announced_output_item:
                    self._announced_output_item = output_item
                    await self.websocket.send_json(
                        {
                            "type": "output_audio.started",
                            "item_id": item_id,
                            "content_index": content_index,
                        }
                    )
            audio_bytes = base64.b64decode(event.delta)
            for offset in range(0, len(audio_bytes), DEVICE_OUTPUT_AUDIO_FRAME_BYTES):
                if not self._response_output_allowed(generation):
                    return
                await self.websocket.send_bytes(
                    audio_bytes[offset : offset + DEVICE_OUTPUT_AUDIO_FRAME_BYTES]
                )

    async def _send_response_json(self, payload: dict[str, Any], generation: int) -> None:
        async with self._downstream_send_lock:
            if self._response_output_allowed(generation):
                await self.websocket.send_json(payload)

    async def _set_response_state(self, state: DeviceState, generation: int) -> bool:
        async with self._downstream_send_lock:
            if not self._response_output_allowed(generation):
                return False
            await self._set_state_locked(state)
            return self._response_output_allowed(generation)

    @staticmethod
    def _playback_position(payload: dict[str, Any]) -> tuple[str, int, int] | None:
        item_id = payload.get("item_id")
        content_index = payload.get("content_index")
        audio_end_ms = payload.get("audio_end_ms")
        if (
            not isinstance(item_id, str)
            or not item_id
            or isinstance(content_index, bool)
            or not isinstance(content_index, int)
            or content_index < 0
            or isinstance(audio_end_ms, bool)
            or not isinstance(audio_end_ms, int)
            or audio_end_ms < 0
        ):
            return None
        return item_id, content_index, audio_end_ms

    def _start_function_call(self, connection: Any, event: Any) -> None:
        generation = self._output_generation
        if not self._response_output_allowed(generation) or not self._response_event_allowed(event):
            return
        self._function_call_seen = True
        self._pending_tool_calls += 1
        task = asyncio.create_task(self._handle_function_call(connection, event, generation))
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def _handle_function_call(self, connection: Any, event: Any, generation: int) -> None:
        cancelled = False
        try:
            if not self._response_output_allowed(generation):
                return
            if event.name == "set_emotion":
                arguments = json.loads(event.arguments)
                emotion = arguments.get("emotion")
                allowed_emotions = {"neutral", "happy", "sad", "angry", "surprised", "sleepy"}
                if emotion not in allowed_emotions:
                    await self._send_function_output(
                        connection, event.call_id, {"error": "invalid emotion"}, generation
                    )
                    return
                await self._send_response_json({"type": "emotion", "emotion": emotion}, generation)
                await self._send_function_output(
                    connection, event.call_id, {"emotion": emotion}, generation
                )
                return

            if self.local_actions.supports(event.name):
                arguments = json.loads(event.arguments)
                result = await self.local_actions.execute(event.name, arguments)
                await self._send_function_output(connection, event.call_id, result, generation)
                await self._send_response_json(
                    {"type": "notice", "title": "LOCAL ACTION", "detail": "Browser opened on PC"},
                    generation,
                )
                return

            if event.name != "search_web":
                await self._send_function_output(
                    connection, event.call_id, {"error": "unsupported tool"}, generation
                )
                return

            arguments = json.loads(event.arguments)
            query = arguments["query"]
            if not await self._set_response_state(DeviceState.SEARCHING, generation):
                return
            # The Responses client owns the HTTP timeout. asyncio.wait_for would
            # only cancel the wrapper around asyncio.to_thread(), leaving the
            # blocking request alive in a worker thread after the Relay reports a
            # false failure.
            result = await self.web_search.search(query)
            await self._send_function_output(
                connection, event.call_id, result.as_tool_output(), generation
            )
            if result.sources:
                await self._send_response_json(
                    SourcesMessage(sources=result.sources).model_dump(), generation
                )
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            logger.exception("function call failed device=%s tool=%s", self.device_id, event.name)
            try:
                await self._send_function_output(
                    connection,
                    event.call_id,
                    {"error": "FUNCTION_CALL_FAILED", "message": str(exc)},
                    generation,
                )
            except Exception:
                logger.debug(
                    "could not return tool failure device=%s", self.device_id, exc_info=True
                )
        finally:
            self._pending_tool_calls -= 1
            if (
                not cancelled
                and self._response_output_allowed(generation)
                and self._pending_tool_calls == 0
                and self._response_done_waiting_for_tools
            ):
                await self._request_tool_followup(connection, generation)

    async def _send_function_output(
        self, connection: Any, call_id: str, output: dict[str, Any], generation: int
    ) -> None:
        async with self._upstream_send_lock:
            if not self._response_output_allowed(generation):
                return
            await connection.conversation.item.create(
                item={
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output, ensure_ascii=False),
                }
            )

    async def _request_tool_followup(
        self, connection: Any, generation: int | None = None
    ) -> None:
        if generation is None:
            generation = self._output_generation
        if not self._response_output_allowed(generation):
            return
        self._function_call_seen = False
        self._response_done_waiting_for_tools = False
        if not await self._set_response_state(DeviceState.THINKING, generation):
            return
        async with self._upstream_send_lock:
            if not self._response_output_allowed(generation):
                return
            await connection.response.create()

    async def _set_state(self, state: DeviceState) -> None:
        async with self._downstream_send_lock:
            await self._set_state_locked(state)

    async def _set_state_locked(self, state: DeviceState) -> None:
        self._state = state
        if state != DeviceState.LISTENING:
            self._clear_queued_audio()
        await self.websocket.send_json({"type": "state", "state": state.value})

    async def _send_error(self, code: str, message: str) -> None:
        await self.websocket.send_json({"type": "error", "code": code, "message": message})
