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
