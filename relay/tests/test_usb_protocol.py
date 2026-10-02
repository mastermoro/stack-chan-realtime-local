from __future__ import annotations

import json

import pytest

from app.usb.bridge import (
    PCM16_MONO_BYTES_PER_SECOND,
    USB_AUDIO_FRAME_BYTES,
    USB_AUDIO_LEAD_SECONDS,
    USB_SERIAL_WRITE_CHUNK_BYTES,
    BridgeMetrics,
    BridgeStatus,
    SerialWorker,
    StatusWriter,
)
from app.usb.protocol import (
    MAX_PAYLOAD_BYTES,
    UsbFrame,
    UsbFrameDecoder,
    UsbFrameType,
    UsbProtocolError,
    cobs_decode,
    cobs_encode,
    decode_frame,
    encode_frame,
)


@pytest.mark.parametrize(
    "payload",
    [b"", b"plain text", b"\x00", bytes(range(256)), b"\x00" * 600],
)
def test_cobs_round_trip(payload: bytes) -> None:
    assert cobs_decode(cobs_encode(payload)) == payload


def test_frame_round_trip_with_partial_stream_reads() -> None:
    expected = UsbFrame(UsbFrameType.DEVICE_BINARY, 42, bytes(range(256)) * 3)
    wire = encode_frame(expected)
    decoder = UsbFrameDecoder()

    frames = []
    for offset in range(0, len(wire), 7):
        result = decoder.feed(wire[offset : offset + 7])
        frames.extend(result.frames)
        assert result.invalid_frames == 0

    assert frames == [expected]


def test_leading_delimiter_recovers_from_unframed_boot_log() -> None:
    expected = UsbFrame(UsbFrameType.HOST_PROBE, 7)
    decoder = UsbFrameDecoder()
    result = decoder.feed(b"ESP-ROM:esp32s3 boot log\n" + encode_frame(expected))

    assert result.frames == [expected]
    assert result.noise == [b"ESP-ROM:esp32s3 boot log\n"]
    assert result.invalid_frames == 1


def test_crc_corruption_is_rejected_and_next_frame_is_decoded() -> None:
    damaged = bytearray(encode_frame(UsbFrame(UsbFrameType.DEVICE_TEXT, 1, b"hello")))
    damaged[5] ^= 0x20
    expected = UsbFrame(UsbFrameType.DEVICE_TEXT, 2, b"world")

    result = UsbFrameDecoder().feed(bytes(damaged) + encode_frame(expected))

    assert result.frames == [expected]
    assert result.invalid_frames == 1


def test_payload_limit_is_enforced() -> None:
    with pytest.raises(UsbProtocolError, match="payload exceeds"):
        encode_frame(UsbFrame(UsbFrameType.DEVICE_BINARY, 0, b"x" * (MAX_PAYLOAD_BYTES + 1)))


def test_decode_rejects_unknown_frame_type() -> None:
    wire = bytearray(encode_frame(UsbFrame(UsbFrameType.HOST_PROBE, 0)))
    # Decode and re-encode is deliberately not used here because CRC must also
    # become invalid when a type byte is altered.
    wire[3] ^= 0x40
    with pytest.raises(UsbProtocolError):
        decode_frame(bytes(wire[1:-1]))


def test_serial_worker_distinguishes_logs_from_binary_corruption() -> None:
    assert SerialWorker._looks_like_log(b"Wi-Fi status: connected\n")
    assert not SerialWorker._looks_like_log(b"\x03\xff\x81\x00\x12\x93")


def test_serial_worker_splits_large_writes_into_timeout_safe_chunks() -> None:
    writes: list[bytes] = []

    class FakePort:
        def write(self, data: bytes) -> int:
            writes.append(data)
            return len(data)

    wire = bytes(range(256)) * 20

    assert SerialWorker._write_wire(FakePort(), wire)  # type: ignore[arg-type]
    assert b"".join(writes) == wire
    assert max(map(len, writes)) <= USB_SERIAL_WRITE_CHUNK_BYTES


def test_usb_audio_frame_is_twenty_milliseconds_of_pcm16() -> None:
    assert USB_AUDIO_FRAME_BYTES / PCM16_MONO_BYTES_PER_SECOND == pytest.approx(0.020)
    assert USB_AUDIO_LEAD_SECONDS == pytest.approx(0.100)


def test_status_writer_persists_bridge_status(tmp_path) -> None:
    path = tmp_path / "bridge.json"

    StatusWriter(path).write(
        BridgeStatus(state="relay_connected", port="COM3", relay_connected=True),
        BridgeMetrics(received_frames=12),
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["state"] == "relay_connected"
    assert payload["port"] == "COM3"
    assert payload["metrics"]["received_frames"] == 12


def test_status_writer_failure_does_not_stop_bridge(tmp_path) -> None:
    unwritable_status_path = tmp_path / "status-directory"
    unwritable_status_path.mkdir()

    StatusWriter(unwritable_status_path).write(BridgeStatus(), BridgeMetrics())


@pytest.mark.asyncio
async def test_bridge_interrupt_discards_paced_and_buffered_output_until_matching_fence():
    import asyncio

    from app.usb.bridge import UsbBridge

    class FakeRelay:
        def __init__(self):
            self.incoming = asyncio.Queue()
            self.sent = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.incoming.get()

        async def send(self, message):
            self.sent.append(message)

    class FakeWorker:
        def __init__(self):
            self.frames = []
            self.audio_started = asyncio.Event()
            self.new_audio = asyncio.Event()

        def send(self, frame_type, payload=b""):
            self.frames.append((frame_type, payload))
            if frame_type == UsbFrameType.HOST_BINARY:
                self.audio_started.set()
                if payload == b"new audio":
                    self.new_audio.set()
            return True

    relay = FakeRelay()
    worker = FakeWorker()
    bridge = UsbBridge("ws://unused")
    bridge._relay = relay
    bridge._worker = worker
    reader = asyncio.create_task(bridge._read_relay())
    try:
        # More than the USB lead: cancellation occurs while this frame is paced.
        await relay.incoming.put(b"a" * 8192)
        await asyncio.wait_for(worker.audio_started.wait(), timeout=1)
        before_cancel = len(worker.frames)
        for interrupt_id in (1, 2):
            await bridge._handle_frame(UsbFrame(
                UsbFrameType.DEVICE_TEXT, interrupt_id,
                json.dumps({"type": "conversation.pause", "interrupt_id": interrupt_id}).encode(),
            ))
        for message in (
            json.dumps({"type": "state", "state": "speaking"}),
            b"old buffered audio",
            json.dumps({"type": "conversation.paused", "interrupt_id": 1}),
            b"old after superseded acknowledgment",
            json.dumps({"type": "conversation.paused", "interrupt_id": 2}),
            json.dumps({"type": "state", "state": "speaking"}),
            b"new audio",
        ):
            await relay.incoming.put(message)
        await asyncio.wait_for(worker.new_audio.wait(), timeout=1)
        forwarded = worker.frames[before_cancel:]
        assert [payload for kind, payload in forwarded if kind == UsbFrameType.HOST_BINARY] == [
            b"new audio"
        ]
        texts = [json.loads(payload) for kind, payload in forwarded
                 if kind == UsbFrameType.HOST_TEXT]
        assert texts == [
            {"type": "conversation.paused", "interrupt_id": 2},
            {"type": "state", "state": "speaking"},
        ]
        assert [json.loads(message)["interrupt_id"] for message in relay.sent] == [1, 2]
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.parametrize("interrupt_id", [None, True, False, 0, -1, 2**32, "1", 1.5])
def test_bridge_ignores_invalid_interrupt_ids(interrupt_id):
    from app.usb.bridge import UsbBridge

    bridge = UsbBridge("ws://unused")
    bridge._observe_device_control(json.dumps({
        "type": "conversation.pause", "interrupt_id": interrupt_id,
    }))
    assert bridge._pending_interrupt is None
    assert not bridge._interrupt.is_set()


def test_bridge_fence_requires_exact_ack_not_state_timer_or_reconnect_notice():
    from types import SimpleNamespace

    from app.usb.bridge import UsbBridge

    bridge = UsbBridge("ws://unused")
    bridge._worker = SimpleNamespace(send=lambda *_args: True)
    bridge._observe_device_control('{"type":"response.cancel","interrupt_id":1}')
    for payload in (
        {"type": "state", "state": "speaking"},
        {"type": "response.done"},
        {"type": "session.reconnected"},
        {"type": "session.ready"},
        {"type": "conversation.paused", "interrupt_id": True},
        {"type": "conversation.paused", "interrupt_id": "1"},
        {"type": "conversation.paused", "interrupt_id": 2},
    ):
        assert not bridge._forward_relay_control(json.dumps(payload))
        assert bridge._pending_interrupt == 1
    assert bridge._forward_relay_control('{"type":"error","code":"UPSTREAM_RECONNECTING"}')
    assert bridge._pending_interrupt == 1
    assert bridge._forward_relay_control('{"type":"conversation.paused","interrupt_id":1}')
    assert bridge._pending_interrupt is None
    assert not bridge._interrupt.is_set()
    assert bridge._forward_relay_control('{"type":"state","state":"speaking"}')


def test_bridge_legacy_client_without_interrupt_id_keeps_forwarding():
    from types import SimpleNamespace

    from app.usb.bridge import UsbBridge

    bridge = UsbBridge("ws://unused")
    bridge._worker = SimpleNamespace(send=lambda *_args: True)
    bridge._observe_device_control('{"type":"conversation.pause"}')
    assert bridge._forward_relay_control('{"type":"state","state":"ready"}')


def test_serial_worker_reads_device_controls_under_continuous_output(monkeypatch):
    """Exercise the real worker loop with a producer that never lets TX empty."""
    from types import SimpleNamespace

    loop = SimpleNamespace(call_soon_threadsafe=lambda callback, value: callback(value))
    worker = SerialWorker("fake", loop)
    pause = UsbFrame(
        UsbFrameType.DEVICE_TEXT, 7,
        b'{"type":"conversation.pause","interrupt_id":1}',
    )

    class FakePort:
        in_waiting = 1

        def __init__(self):
            self.writes = 0
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def write(self, data):
            self.writes += 1
            if self.writes > 32:
                raise AssertionError("device input starved behind continuous serial output")
            # Model a slow writer while Relay keeps replenishing its queue.
            assert worker.send(UsbFrameType.HOST_BINARY, b"next audio")
            return len(data)

        def read(self, _size):
            self.reads += 1
            worker._stop.set()
            return encode_frame(pause)

    port = FakePort()
    monkeypatch.setattr(worker, "_open_port", lambda: port)
    worker.send(UsbFrameType.HOST_BINARY, b"first audio")
    worker._run()
    events = []
    while not worker.events.empty():
        events.append(worker.events.get_nowait())
    assert port.reads == 1
    assert port.writes <= 4
    assert any(event.kind == "frame" and event.value == pause for event in events)


@pytest.mark.asyncio
async def test_bridge_failed_fence_enqueue_stays_fenced_and_closes_relay():
    from types import SimpleNamespace

    from app.usb.bridge import UsbBridge

    class FakeRelay:
        closed = False

        async def close(self):
            self.closed = True

        async def __aiter__(self):
            yield '{"type":"conversation.paused","interrupt_id":42}'
            yield b"must not forward after losing the fence"

    relay = FakeRelay()
    frames = []
    bridge = UsbBridge("ws://unused")
    bridge._relay = relay
    bridge._worker = SimpleNamespace(send=lambda kind, payload: frames.append((kind, payload)))
    bridge._observe_device_control('{"type":"conversation.pause","interrupt_id":42}')
    await bridge._read_relay()
    assert bridge._pending_interrupt == 42
    assert bridge._interrupt.is_set()
    assert relay.closed
    assert bridge._relay is None
    assert not any(kind == UsbFrameType.HOST_BINARY for kind, _ in frames)
    assert bridge._metrics.transmitted_frames == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_id", [None, 42])
@pytest.mark.parametrize("enqueue_success", [False, True])
async def test_bridge_recovery_ack_follows_new_relay_connection(
    monkeypatch, recovery_id, enqueue_success,
):
    import asyncio
    from types import SimpleNamespace

    from app.usb.bridge import UsbBridge

    class FakeRelay:
        closed = False

        async def close(self):
            self.closed = True

        async def __aiter__(self):
            await asyncio.Event().wait()
            yield ""

    old_relay = FakeRelay()
    new_relay = FakeRelay()
    bridge = UsbBridge("ws://unused")
    bridge._device_id = "test-device"
    bridge._relay = old_relay
    frames = []

    def send(kind, payload=b""):
        frames.append((kind, payload))
        return enqueue_success

    async def connect(*_args, **_kwargs):
        assert old_relay.closed
        assert not frames
        return new_relay

    bridge._worker = SimpleNamespace(send=send)
    monkeypatch.setattr("app.usb.bridge.connect", connect)
    payload = {"device_id": "test-device", "device_token": "dummy-test-token"}
    if recovery_id is not None:
        payload["recovery_id"] = recovery_id
    try:
        await bridge._open_relay(json.dumps(payload).encode())
        acknowledgments = [data for kind, data in frames if kind == UsbFrameType.HOST_OPEN_ACK]
        assert len(acknowledgments) == 1
        assert (json.loads(acknowledgments[0]) if recovery_id else acknowledgments[0]) == (
            {"recovery_id": recovery_id} if recovery_id else b""
        )
        if enqueue_success:
            assert bridge._relay is new_relay
        else:
            assert bridge._relay is None
            assert bridge._relay_reader is None
            assert new_relay.closed
    finally:
        await bridge._close_relay(send_close=False)
    assert new_relay.closed
