from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum

PROTOCOL_VERSION = 1
MAX_PAYLOAD_BYTES = 8192
_HEADER = struct.Struct("<BBHI")
_CRC = struct.Struct("<I")


class UsbFrameType(IntEnum):
    HOST_PROBE = 0x01
    DEVICE_STATUS = 0x02
    DEVICE_OPEN = 0x03
    HOST_OPEN_ACK = 0x04
    DEVICE_CLOSE = 0x05
    HOST_CLOSE = 0x06
    DEVICE_TEXT = 0x10
    DEVICE_BINARY = 0x11
    HOST_TEXT = 0x20
    HOST_BINARY = 0x21
    DEVICE_LOG = 0x30
    ERROR = 0x7F


@dataclass(frozen=True, slots=True)
class UsbFrame:
    frame_type: UsbFrameType
    sequence: int
    payload: bytes = b""


@dataclass(frozen=True, slots=True)
class DecodeResult:
    frames: list[UsbFrame]
    noise: list[bytes]
    invalid_frames: int


class UsbProtocolError(ValueError):
    pass


def cobs_encode(data: bytes) -> bytes:
    output = bytearray()
    code_index = 0
    output.append(0)
    code = 1

    for value in data:
        if value == 0:
            output[code_index] = code
            code_index = len(output)
            output.append(0)
            code = 1
            continue
        output.append(value)
        code += 1
        if code == 0xFF:
            output[code_index] = code
            code_index = len(output)
            output.append(0)
            code = 1

    output[code_index] = code
    return bytes(output)


def cobs_decode(data: bytes) -> bytes:
    if not data:
        raise UsbProtocolError("empty COBS payload")

    output = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        if code == 0:
            raise UsbProtocolError("zero byte inside COBS payload")
        index += 1
        end = index + code - 1
        if end > len(data):
            raise UsbProtocolError("truncated COBS payload")
        output.extend(data[index:end])
        index = end
        if code != 0xFF and index < len(data):
            output.append(0)
    return bytes(output)


def encode_frame(frame: UsbFrame) -> bytes:
    payload = frame.payload
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise UsbProtocolError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    if not 0 <= frame.sequence <= 0xFFFF:
        raise UsbProtocolError("sequence must fit in uint16")

    header = _HEADER.pack(
        PROTOCOL_VERSION,
        int(frame.frame_type),
        frame.sequence,
        len(payload),
    )
    body = header + payload
    checksum = _CRC.pack(zlib.crc32(body) & 0xFFFFFFFF)
    # The leading delimiter flushes any unframed boot/framework log bytes before
    # this packet. The trailing delimiter completes the COBS packet.
    return b"\x00" + cobs_encode(body + checksum) + b"\x00"


def decode_frame(encoded: bytes) -> UsbFrame:
    decoded = cobs_decode(encoded)
    minimum_size = _HEADER.size + _CRC.size
    if len(decoded) < minimum_size:
        raise UsbProtocolError("frame is shorter than its header and CRC")

    body = decoded[:-_CRC.size]
    expected_crc = _CRC.unpack(decoded[-_CRC.size:])[0]
    actual_crc = zlib.crc32(body) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise UsbProtocolError("CRC mismatch")

    version, raw_type, sequence, payload_size = _HEADER.unpack(body[: _HEADER.size])
    if version != PROTOCOL_VERSION:
        raise UsbProtocolError(f"unsupported protocol version: {version}")
    payload = body[_HEADER.size :]
    if payload_size != len(payload):
        raise UsbProtocolError("payload length mismatch")
    if payload_size > MAX_PAYLOAD_BYTES:
        raise UsbProtocolError("payload exceeds protocol maximum")
    try:
        frame_type = UsbFrameType(raw_type)
    except ValueError as exc:
        raise UsbProtocolError(f"unknown frame type: {raw_type}") from exc
    return UsbFrame(frame_type=frame_type, sequence=sequence, payload=payload)


class UsbFrameDecoder:
    def __init__(self, *, max_encoded_bytes: int = MAX_PAYLOAD_BYTES + 128) -> None:
        self._buffer = bytearray()
        self._max_encoded_bytes = max_encoded_bytes
        self._discarding = False

    def feed(self, data: bytes) -> DecodeResult:
        frames: list[UsbFrame] = []
        noise: list[bytes] = []
        invalid_frames = 0

        for value in data:
            if value != 0:
                if self._discarding:
                    continue
                self._buffer.append(value)
                if len(self._buffer) > self._max_encoded_bytes:
                    noise.append(bytes(self._buffer))
                    self._buffer.clear()
                    self._discarding = True
                continue

            if self._discarding:
                self._discarding = False
                continue
            if not self._buffer:
                continue

            candidate = bytes(self._buffer)
            self._buffer.clear()
            try:
                frames.append(decode_frame(candidate))
            except UsbProtocolError:
                noise.append(candidate)
                invalid_frames += 1

        return DecodeResult(frames=frames, noise=noise, invalid_frames=invalid_frames)
