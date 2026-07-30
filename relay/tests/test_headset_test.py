import asyncio
import json
from pathlib import Path

import pytest

from app.config.settings import Settings
from headset_test import (
    PlaybackFrame,
    audio_duration_ms,
    can_send_resume,
    captures_audio,
    device_credentials,
    enqueue_latest_audio,
    enqueue_playback_audio,
    format_relay_event,
    interruption_payload,
    parse_args,
    read_control,
    should_resume_after_reconnect,
    should_resume_listening,
    write_status,
)


def test_device_credentials_uses_first_configured_device() -> None:
    settings = Settings(device_tokens_json={"stackchan-001": "token"})

    assert device_credentials(settings) == ("stackchan-001", "token")


def test_device_credentials_rejects_unknown_device() -> None:
    settings = Settings(device_tokens_json={"stackchan-001": "token"})

    with pytest.raises(ValueError, match="No device token"):
        device_credentials(settings, "other-device")


def test_parse_args_accepts_audio_device_indexes() -> None:
    args = parse_args(["--input-device", "2", "--output-device", "4"])

    assert args.input_device == 2
    assert args.output_device == 4


def test_audio_capture_is_enabled_only_while_listening() -> None:
    assert captures_audio("listening")
    assert not captures_audio("ready")
    assert not captures_audio("thinking")
    assert not captures_audio("speaking")


def test_completed_response_restarts_only_an_active_conversation() -> None:
    assert should_resume_listening(
        state="ready", conversation_active=True, response_completed=True
    )
    assert not should_resume_listening(
        state="speaking", conversation_active=True, response_completed=True
    )
    assert not should_resume_listening(
        state="ready", conversation_active=False, response_completed=True
    )


def test_pause_invalidates_an_auto_resume_already_waiting_for_playback() -> None:
    assert not can_send_resume(
        state="ready", conversation_active=False, expected_revision=3, revision=4
    )
    assert not can_send_resume(
        state="ready", conversation_active=True, expected_revision=3, revision=4
    )
    assert can_send_resume(
        state="ready", conversation_active=True, expected_revision=3, revision=3
    )


def test_only_active_conversations_resume_after_an_upstream_reconnect() -> None:
    assert should_resume_after_reconnect(True)
    assert not should_resume_after_reconnect(False)


def test_write_status_makes_current_conversation_state_available(tmp_path: Path) -> None:
    status_file = tmp_path / "headset-status.json"

    assert write_status(
        status_file,
        state="speaking",
        capture_enabled=False,
        conversation_active=True,
    )

    status = json.loads(status_file.read_text(encoding="utf-8"))
    assert status["state"] == "speaking"
    assert status["capture_enabled"] is False
    assert status["conversation_active"] is True


def test_status_write_failure_does_not_raise_or_end_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status_file = tmp_path / "headset-status.json"

    def deny_write(_: Path, *__: object, **___: object) -> int:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "write_text", deny_write)

    assert not write_status(
        status_file,
        state="speaking",
        capture_enabled=False,
        conversation_active=True,
    )


def test_read_control_accepts_only_known_manager_actions(tmp_path: Path) -> None:
    control_file = tmp_path / "headset-control.json"
    control_file.write_text('{"action":"start","request_id":"request-1"}', encoding="utf-8")

    assert read_control(control_file) == ("start", "request-1")

    control_file.write_text('{"action":"unknown","request_id":"request-2"}', encoding="utf-8")
    assert read_control(control_file) is None


def test_audio_queue_discards_the_oldest_frame_when_full() -> None:
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1)

    assert enqueue_latest_audio(queue, b"first")
    assert not enqueue_latest_audio(queue, b"second")
    assert queue.get_nowait() == b"second"


def test_playback_queue_preserves_every_frame() -> None:
    queue: asyncio.Queue[PlaybackFrame | None] = asyncio.Queue()
    first = PlaybackFrame(audio=b"first", item_id="item-1")
    second = PlaybackFrame(audio=b"second", item_id="item-1")

    assert enqueue_playback_audio(queue, first) == 1
    assert enqueue_playback_audio(queue, second) == 2
    assert [queue.get_nowait(), queue.get_nowait()] == [first, second]


def test_interruption_payload_tracks_played_audio_and_omits_unknown_item() -> None:
    assert interruption_payload(item_id="item-1", content_index=0, audio_end_ms=1480) == {
        "type": "conversation.pause",
        "item_id": "item-1",
        "content_index": 0,
        "audio_end_ms": 1480,
    }
    assert interruption_payload(item_id=None, content_index=0, audio_end_ms=0) == {
        "type": "conversation.pause"
    }


def test_pcm_audio_duration_is_based_on_the_wire_format() -> None:
    assert audio_duration_ms(bytes(960)) == 20


def test_relay_event_format_is_safe_for_cp932_logs() -> None:
    event = format_relay_event({"type": "sources", "title": "Beyoncé"})

    assert event.encode("cp932", errors="strict")
    assert "Beyonc\\u00e9" in event
