from __future__ import annotations

import argparse
import asyncio
import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.config.settings import Settings

try:
    import sounddevice as sd
except ModuleNotFoundError:  # Optional unless the headset client is started.
    sd = None  # type: ignore[assignment]

SAMPLE_RATE = 24000
CHANNELS = 1
FRAME_SAMPLES = 480
# 20 ms frames. Keep local audio live rather than replaying buffered speech.
MICROPHONE_QUEUE_FRAMES = 3
PLAYBACK_BACKLOG_WARNING_FRAMES = 100
# A control command is local IPC, so poll at the audio-frame cadence.  This
# keeps the stop button responsive without adding another local server.
CONTROL_POLL_INTERVAL_SECONDS = 0.02


@dataclass(frozen=True)
class PlaybackFrame:
    """One Relay audio frame plus the Realtime item that produced it."""

    audio: bytes
    item_id: str | None
    content_index: int = 0


def captures_audio(state: str) -> bool:
    return state == "listening"


def should_resume_listening(
    *, state: str, conversation_active: bool, response_completed: bool
) -> bool:
    """Return whether the next turn should begin after a completed response."""
    return conversation_active and response_completed and state == "ready"


def can_send_resume(
    *, state: str, conversation_active: bool, expected_revision: int, revision: int
) -> bool:
    """Reject an auto-resume that was overtaken by start/pause control."""
    return conversation_active and state == "ready" and revision == expected_revision


def should_resume_after_reconnect(conversation_active: bool) -> bool:
    """Resume a still-active conversation after Relay opens a new upstream session."""
    return conversation_active


def write_status(
    status_file: Path | None,
    *,
    state: str,
    capture_enabled: bool,
    conversation_active: bool,
) -> bool:
    """Best-effort status publication that can never terminate the audio session."""
    if status_file is None:
        return False
    payload = {
        "state": state,
        "capture_enabled": capture_enabled,
        "conversation_active": conversation_active,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    try:
        status_file.parent.mkdir(parents=True, exist_ok=True)
        # Windows can deny an atomic replacement while the Manager is reading the
        # file. A partial read is harmless (the reader already tolerates invalid
        # JSON), but status reporting must never stop the conversation process.
        status_file.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return False
    return True


def read_control(control_file: Path | None) -> tuple[str, str] | None:
    """Read the newest manager command, if one has been published."""
    if control_file is None:
        return None
    try:
        payload = json.loads(control_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    action = payload.get("action")
    request_id = payload.get("request_id")
    if action in {"start", "pause"} and isinstance(request_id, str):
        return action, request_id
    return None


def enqueue_latest_audio(queue: asyncio.Queue[bytes | None], chunk: bytes) -> bool:
    """Keep the most recent audio when a bounded queue is temporarily full."""
    try:
        queue.put_nowait(chunk)
    except asyncio.QueueFull:
        # Older audio would only increase end-to-end latency. Discard it first so
        # playback/capture catches up instead of processing stale speech later.
        queue.get_nowait()
        queue.put_nowait(chunk)
        return False
    return True


def enqueue_playback_audio(
    queue: asyncio.Queue[PlaybackFrame | None], frame: PlaybackFrame
) -> int:
    """Preserve every returned audio frame and report the queued frame count."""
    queue.put_nowait(frame)
    return queue.qsize()


def audio_duration_ms(audio: bytes) -> int:
    """Return a PCM16 frame duration, rounded to the nearest millisecond."""
    bytes_per_second = SAMPLE_RATE * CHANNELS * 2
    return round(len(audio) * 1000 / bytes_per_second)


def interruption_payload(
    *, item_id: str | None, content_index: int, audio_end_ms: int
) -> dict[str, Any]:
    """Build a pause command with the client-managed playback position."""
    payload: dict[str, Any] = {"type": "conversation.pause"}
    if item_id is not None:
        payload.update(
            {
                "item_id": item_id,
                "content_index": content_index,
                "audio_end_ms": max(audio_end_ms, 0),
            }
        )
    return payload


def format_relay_event(payload: dict[str, Any]) -> str:
    """Format events safely for Windows consoles and redirected CP932 logs."""
    return f"relay event: {json.dumps(payload, ensure_ascii=True)}"


def device_credentials(
    settings: Settings, requested_device_id: str | None = None
) -> tuple[str, str]:
    if not settings.device_tokens_json:
        raise ValueError("DEVICE_TOKENS_JSON must contain at least one device token")
    device_id = requested_device_id or next(iter(settings.device_tokens_json))
    token = settings.device_tokens_json.get(device_id)
    if not token:
        raise ValueError(f"No device token is configured for {device_id!r}")
    return device_id, token


async def run_headset_test(
    *,
    relay_url: str,
    device_id: str,
    device_token: str,
    input_device: int | None,
    output_device: int | None,
    status_file: Path | None = None,
    control_file: Path | None = None,
) -> None:
    headers = {
        "Authorization": f"Bearer {device_token}",
        "X-Device-Id": device_id,
    }
    hello = {
        "type": "hello",
        "protocol": 1,
        "device_id": device_id,
        "audio": {"format": "pcm16", "sample_rate": SAMPLE_RATE, "channels": CHANNELS},
    }
    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=MICROPHONE_QUEUE_FRAMES)
    capture_enabled = threading.Event()
    capture_lock = threading.Lock()
    pending_capture: bytes | None = None
    capture_enqueue_scheduled = False
    loop = asyncio.get_running_loop()
    last_state = ""
    conversation_active = False
    conversation_revision = 0
    response_completed = False
    current_state = "connecting"
    write_status(
        status_file,
        state=current_state,
        capture_enabled=False,
        conversation_active=conversation_active,
    )

    def clear_audio_queue() -> None:
        while not audio_queue.empty():
            audio_queue.get_nowait()

        nonlocal pending_capture
        with capture_lock:
            pending_capture = None

    def enqueue_audio(chunk: bytes) -> None:
        if not capture_enabled.is_set():
            return

        enqueue_latest_audio(audio_queue, chunk)

    def flush_captured_audio() -> None:
        nonlocal pending_capture, capture_enqueue_scheduled
        with capture_lock:
            chunk = pending_capture
            pending_capture = None
            capture_enqueue_scheduled = False
        if chunk is not None:
            enqueue_audio(chunk)

    def on_input(indata: Any, frames: int, _: Any, status: Any) -> None:
        nonlocal pending_capture, capture_enqueue_scheduled
        if not capture_enabled.is_set():
            return
        if status:
            print(f"audio input status: {status}", flush=True)
        if frames != FRAME_SAMPLES:
            print(f"audio input frame size: {frames}", flush=True)
        # Never build an unbounded loop callback backlog from the PortAudio
        # thread. If the loop is busy, retain only the newest frame.
        with capture_lock:
            pending_capture = bytes(indata)
            if capture_enqueue_scheduled:
                return
            capture_enqueue_scheduled = True
        loop.call_soon_threadsafe(flush_captured_audio)

    async with connect(relay_url, additional_headers=headers, max_size=None) as websocket:
        send_lock = asyncio.Lock()

        async def send_frame(payload: str | bytes) -> None:
            async with send_lock:
                await websocket.send(payload)

        await send_frame(json.dumps(hello))
        print(f"connected to Relay as {device_id}; waiting for conversation start", flush=True)

        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            dtype="int16",
            channels=CHANNELS,
            device=input_device,
            callback=on_input,
        ), sd.RawOutputStream(
            samplerate=SAMPLE_RATE,
            dtype="int16",
            channels=CHANNELS,
            device=output_device,
        ) as output_stream:
            # Output is the assistant's answer, so it must never be discarded.
            # Keep every frame for one response; listening is not resumed until
            # the player drains this queue.
            playback_queue: asyncio.Queue[PlaybackFrame | None] = asyncio.Queue()
            playback_idle = asyncio.Event()
            playback_idle.set()
            active_output_item_id: str | None = None
            active_output_content_index = 0
            played_output_item_id: str | None = None
            played_output_content_index = 0
            played_output_ms = 0

            async def send_microphone_audio() -> None:
                while True:
                    chunk = await audio_queue.get()
                    if chunk is None:
                        return
                    await send_frame(chunk)

            async def play_returned_audio() -> None:
                nonlocal played_output_content_index, played_output_item_id, played_output_ms
                output_failed = False
                while True:
                    frame = await playback_queue.get()
                    if frame is None:
                        playback_idle.set()
                        return
                    if output_failed:
                        continue
                    # sounddevice.write() can wait for the audio device. Run it away from
                    # the asyncio event loop so WebSocket pong frames are never delayed.
                    try:
                        await asyncio.to_thread(output_stream.write, frame.audio)
                    except Exception as exc:
                        output_failed = True
                        playback_idle.set()
                        print(f"audio output disabled after device error: {exc}", flush=True)
                    else:
                        if frame.item_id != played_output_item_id:
                            played_output_item_id = frame.item_id
                            played_output_content_index = frame.content_index
                            played_output_ms = 0
                        played_output_ms += audio_duration_ms(frame.audio)
                    if playback_queue.empty():
                        playback_idle.set()

            def clear_playback_queue() -> int:
                """Discard only unplayed output after an explicit user interruption."""
                discarded_frames = 0
                while not playback_queue.empty():
                    playback_queue.get_nowait()
                    discarded_frames += 1
                playback_idle.set()
                return discarded_frames

            async def process_manager_controls() -> None:
                nonlocal conversation_active, conversation_revision
                nonlocal response_completed, current_state
                last_request_id = ""
                while True:
                    command = read_control(control_file)
                    if command is not None:
                        action, request_id = command
                        if request_id != last_request_id:
                            last_request_id = request_id
                            if action == "start" and not conversation_active:
                                conversation_active = True
                                conversation_revision += 1
                                response_completed = False
                                await send_frame(json.dumps({"type": "audio.start"}))
                                print("conversation started", flush=True)
                            elif action == "pause" and conversation_active:
                                conversation_active = False
                                conversation_revision += 1
                                response_completed = False
                                capture_enabled.clear()
                                clear_audio_queue()
                                if playback_idle.is_set():
                                    pause_payload = interruption_payload(
                                        item_id=None, content_index=0, audio_end_ms=0
                                    )
                                else:
                                    pause_item_id = played_output_item_id or active_output_item_id
                                    pause_content_index = (
                                        played_output_content_index
                                        if played_output_item_id is not None
                                        else active_output_content_index
                                    )
                                    pause_audio_end_ms = (
                                        played_output_ms
                                        - round(float(output_stream.latency) * 1000)
                                        if played_output_item_id is not None
                                        else 0
                                    )
                                    pause_payload = interruption_payload(
                                        item_id=pause_item_id,
                                        content_index=pause_content_index,
                                        audio_end_ms=pause_audio_end_ms,
                                    )
                                discarded_frames = clear_playback_queue()
                                current_state = "ready"
                                # A WebSocket client owns audio playback.  Tell Relay exactly
                                # where playback stopped so it can cancel the response and
                                # truncate the unheard part of the conversation item.
                                await send_frame(json.dumps(pause_payload))
                                print(
                                    "conversation paused; stopped local output "
                                    f"and discarded {discarded_frames} queued frames",
                                    flush=True,
                                )
                            write_status(
                                status_file,
                                state=current_state,
                                capture_enabled=capture_enabled.is_set(),
                                conversation_active=conversation_active,
                            )
                    await asyncio.sleep(CONTROL_POLL_INTERVAL_SECONDS)

            sender = asyncio.create_task(send_microphone_audio())
            controller = asyncio.create_task(process_manager_controls())
            player = asyncio.create_task(play_returned_audio())
            try:
                async for message in websocket:
                    if isinstance(message, bytes):
                        # Cancellation can race with a few already-in-flight Relay frames.
                        # They belong to the interrupted answer and must not restart output.
                        if not conversation_active:
                            continue
                        playback_idle.clear()
                        queued_frames = enqueue_playback_audio(
                            playback_queue,
                            PlaybackFrame(
                                audio=message,
                                item_id=active_output_item_id,
                                content_index=active_output_content_index,
                            ),
                        )
                        if (
                            queued_frames >= PLAYBACK_BACKLOG_WARNING_FRAMES
                            and queued_frames % PLAYBACK_BACKLOG_WARNING_FRAMES == 0
                        ):
                            print(
                                "audio output backlog: "
                                f"{queued_frames} frames; preserving all audio",
                                flush=True,
                            )
                    else:
                        payload = json.loads(message)
                        message_type = payload.get("type")
                        if message_type == "output_audio.started":
                            item_id = payload.get("item_id")
                            content_index = payload.get("content_index")
                            if isinstance(item_id, str) and isinstance(content_index, int):
                                active_output_item_id = item_id
                                active_output_content_index = content_index
                        elif message_type == "session.reconnected":
                            # A Foundry reconnect has no response.done event for the
                            # previous socket. Ask for a fresh listening turn once
                            # the following ready state arrives.
                            response_completed = should_resume_after_reconnect(
                                conversation_active
                            )
                        elif message_type == "response.done":
                            response_completed = True
                        state = payload.get("state")
                        if isinstance(state, str):
                            current_state = state
                            if state != last_state:
                                print(f"relay state: {state}", flush=True)
                                last_state = state
                            if conversation_active and captures_audio(state):
                                capture_enabled.set()
                            else:
                                capture_enabled.clear()
                                clear_audio_queue()
                            write_status(
                                status_file,
                                state=state,
                                capture_enabled=capture_enabled.is_set(),
                                conversation_active=conversation_active,
                            )
                            if should_resume_listening(
                                state=state,
                                conversation_active=conversation_active,
                                response_completed=response_completed,
                            ):
                                response_completed = False
                                resume_revision = conversation_revision
                                await playback_idle.wait()
                                await asyncio.sleep(max(float(output_stream.latency), 0.05))
                                if not can_send_resume(
                                    state=current_state,
                                    conversation_active=conversation_active,
                                    expected_revision=resume_revision,
                                    revision=conversation_revision,
                                ):
                                    continue
                                await send_frame(json.dumps({"type": "audio.start"}))
                                print("response complete; listening for the next turn", flush=True)
                        else:
                            print(format_relay_event(payload), flush=True)
            finally:
                capture_enabled.clear()
                write_status(
                    status_file,
                    state=current_state,
                    capture_enabled=False,
                    conversation_active=False,
                )
                sender.cancel()
                controller.cancel()
                clear_audio_queue()
                clear_playback_queue()
                playback_queue.put_nowait(None)
                await asyncio.gather(sender, controller, return_exceptions=True)
                # Do not close the PortAudio stream until its final write returns.
                await asyncio.gather(player, return_exceptions=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test the Stack-chan Relay with a local headset")
    parser.add_argument("--relay-url", default="ws://127.0.0.1:8080/v1/realtime")
    parser.add_argument("--device-id")
    parser.add_argument("--input-device", type=int)
    parser.add_argument("--output-device", type=int)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--control-file", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if sd is None:
        raise SystemExit(
            "The headset dependency is not installed. Run setup-windows.ps1 -WithHeadset."
        )
    settings = Settings()
    if not settings.foundry_configured:
        raise SystemExit(
            "Foundry is not configured. Set a real endpoint and deployment names in relay/.env."
        )
    device_id, token = device_credentials(settings, args.device_id)
    try:
        asyncio.run(
            run_headset_test(
                relay_url=args.relay_url,
                device_id=device_id,
                device_token=token,
                input_device=args.input_device,
                output_device=args.output_device,
                status_file=args.status_file,
                control_file=args.control_file,
            )
        )
    except KeyboardInterrupt:
        print("headset test stopped", flush=True)
    except ConnectionClosed as exc:
        print(f"Relay connection closed: {exc}", flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
