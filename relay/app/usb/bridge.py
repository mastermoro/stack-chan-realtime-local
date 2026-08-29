from __future__ import annotations

import argparse
import asyncio
import json
import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import serial
from serial.tools import list_ports
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.usb.protocol import (
    MAX_PAYLOAD_BYTES,
    UsbFrame,
    UsbFrameDecoder,
    UsbFrameType,
    encode_frame,
)

logger = logging.getLogger(__name__)

ESPRESSIF_USB_VID = 0x303A
PROBE_INTERVAL_SECONDS = 0.5
DEVICE_DISCOVERY_TIMEOUT_SECONDS = 12.0
PCM16_MONO_BYTES_PER_SECOND = 24_000 * 2
USB_AUDIO_FRAME_BYTES = 960  # 20 ms at 24 kHz PCM16 mono
# Keep 100 ms queued ahead of playback. This is only about 4.8 KiB of PCM,
# safely below the device's 16 KiB HWCDC RX buffer, and absorbs ordinary
# Windows scheduler jitter without the previous 8 KiB unpaced bursts.
USB_AUDIO_LEAD_SECONDS = 0.100


@dataclass(slots=True)
class BridgeMetrics:
    received_frames: int = 0
    transmitted_frames: int = 0
    invalid_frames: int = 0
    sequence_gaps: int = 0
    raw_log_bytes: int = 0
    reconnects: int = 0


@dataclass(slots=True)
class BridgeStatus:
    state: str = "scanning"
    port: str | None = None
    device_id: str | None = None
    relay_connected: bool = False
    last_error: str | None = None
    updated_at: float = 0.0
    metrics: BridgeMetrics | None = None


@dataclass(frozen=True, slots=True)
class SerialEvent:
    kind: str
    value: UsbFrame | bytes | str


class StatusWriter:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._last_write = 0.0
        self._last_state: tuple[object, ...] | None = None
        self._last_warning = 0.0

    def write(self, status: BridgeStatus, metrics: BridgeMetrics) -> None:
        if self._path is None:
            return

        now = time.monotonic()
        state = (
            status.state,
            status.port,
            status.device_id,
            status.relay_connected,
            status.last_error,
        )
        with self._lock:
            # Audio forwarding used to rewrite this file for every 20 ms USB
            # frame. Besides blocking the event loop, Path.replace can fail on
            # Windows while the manager is reading the destination. Status is
            # advisory, so cap metric-only updates and never let an OS-level
            # file sharing error terminate the bridge.
            if state == self._last_state and now - self._last_write < 0.25:
                return

            status.updated_at = time.time()
            status.metrics = metrics
            payload = json.dumps(asdict(status), ensure_ascii=False, separators=(",", ":"))
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # A short direct write is more reliable than rename-overwrite
                # on Windows. The manager already treats a transient partial
                # JSON read as unavailable and retries on its next poll.
                self._path.write_text(payload, encoding="utf-8")
            except OSError as exc:
                if now - self._last_warning >= 5.0:
                    logger.warning("USB bridge status write failed: %s", exc)
                    self._last_warning = now
                return

            self._last_write = now
            self._last_state = state


class SerialWorker:
    def __init__(self, port_name: str, loop: asyncio.AbstractEventLoop) -> None:
        self.port_name = port_name
        self._loop = loop
        self.events: asyncio.Queue[SerialEvent] = asyncio.Queue()
        self._outgoing: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"stackchan-usb-{port_name}",
            daemon=True,
        )
        self._sequence = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def send(self, frame_type: UsbFrameType, payload: bytes = b"") -> bool:
        frame = UsbFrame(frame_type=frame_type, sequence=self._sequence, payload=payload)
        self._sequence = (self._sequence + 1) & 0xFFFF
        try:
            self._outgoing.put_nowait(encode_frame(frame))
        except queue.Full:
            return False
        return True

    def _emit(self, event: SerialEvent) -> None:
        self._loop.call_soon_threadsafe(self.events.put_nowait, event)

    def _open_port(self) -> serial.Serial:
        port = serial.Serial()
        port.port = self.port_name
        port.baudrate = 115200
        port.timeout = 0.005
        port.write_timeout = 0.25
        # Avoid intentionally requesting the USB Serial/JTAG reset/download
        # line states. The bridge still tolerates a one-time re-enumeration.
        port.dtr = False
        port.rts = False
        port.open()
        return port

    def _run(self) -> None:
        decoder = UsbFrameDecoder()
        next_probe = 0.0
        try:
            with self._open_port() as port:
                self._emit(SerialEvent("opened", self.port_name))
                while not self._stop.is_set():
                    now = time.monotonic()
                    if now >= next_probe:
                        self.send(UsbFrameType.HOST_PROBE)
                        next_probe = now + PROBE_INTERVAL_SECONDS

                    while True:
                        try:
                            wire = self._outgoing.get_nowait()
                        except queue.Empty:
                            break
                        offset = 0
                        while offset < len(wire):
                            written = port.write(wire[offset:])
                            if written <= 0:
                                self._emit(SerialEvent("error", "serial write made no progress"))
                                return
                            offset += written

                    incoming = port.read(max(port.in_waiting, 1))
                    if not incoming:
                        continue
                    result = decoder.feed(incoming)
                    for frame in result.frames:
                        self._emit(SerialEvent("frame", frame))
                    binary_noise = 0
                    for noise in result.noise:
                        self._emit(SerialEvent("noise", noise))
                        if not self._looks_like_log(noise):
                            binary_noise += 1
                    if binary_noise:
                        self._emit(SerialEvent("invalid", str(binary_noise)))
        except (OSError, serial.SerialException) as exc:
            self._emit(SerialEvent("error", str(exc)))
        finally:
            self._emit(SerialEvent("closed", self.port_name))

    @staticmethod
    def _looks_like_log(data: bytes) -> bool:
        if not data:
            return False
        printable = sum(value in {9, 10, 13} or 32 <= value < 127 for value in data)
        return printable / len(data) >= 0.85


class UsbBridge:
    def __init__(self, relay_url: str, status_path: Path | None = None) -> None:
        self._relay_url = relay_url
        self._status_writer = StatusWriter(status_path)
        self._status = BridgeStatus()
        self._metrics = BridgeMetrics()
        self._relay: ClientConnection | None = None
        self._relay_reader: asyncio.Task[None] | None = None
        self._worker: SerialWorker | None = None
        self._device_id: str | None = None
        self._expected_device_sequence: int | None = None

    async def run(self) -> None:
        self._write_status()
        while True:
            candidates = self._candidate_ports()
            if not candidates:
                self._set_status(state="scanning", port=None, device_id=None, last_error=None)
                await asyncio.sleep(1)
                continue

            claimed = False
            for port_name in candidates:
                if await self._try_port(port_name):
                    claimed = True
                    break
            if not claimed:
                await asyncio.sleep(1)

    @staticmethod
    def _candidate_ports() -> list[str]:
        candidates = [
            port.device
            for port in list_ports.comports()
            if port.vid == ESPRESSIF_USB_VID
        ]
        return sorted(set(candidates))

    async def _try_port(self, port_name: str) -> bool:
        loop = asyncio.get_running_loop()
        worker = SerialWorker(port_name, loop)
        self._worker = worker
        self._device_id = None
        self._expected_device_sequence = None
        self._set_status(state="probing", port=port_name, device_id=None, last_error=None)
        worker.start()
        deadline = loop.time() + DEVICE_DISCOVERY_TIMEOUT_SECONDS
        recognized = False
        try:
            while loop.time() < deadline:
                timeout = max(deadline - loop.time(), 0.01)
                try:
                    event = await asyncio.wait_for(worker.events.get(), timeout=timeout)
                except TimeoutError:
                    break
                if event.kind == "frame":
                    frame = event.value
                    assert isinstance(frame, UsbFrame)
                    if frame.frame_type == UsbFrameType.DEVICE_STATUS:
                        first_status = not recognized
                        recognized = self._handle_device_status(frame)
                        if recognized and self._relay is None:
                            self._set_status(state="ready", device_id=self._device_id)
                        if recognized and first_status:
                            # A restarted bridge has lost the old PC WebSocket
                            # even if its first probe arrives before the device's
                            # host-presence timer expires. Force a fresh OPEN.
                            self._send(UsbFrameType.HOST_CLOSE, b"bridge synchronized")
                    elif recognized:
                        await self._handle_frame(frame)
                elif event.kind == "noise":
                    self._handle_noise(event.value)
                elif event.kind == "invalid":
                    self._metrics.invalid_frames += int(str(event.value))
                elif event.kind in {"error", "closed"}:
                    if event.kind == "error":
                        self._set_status(last_error=str(event.value))
                    return recognized

                if recognized:
                    deadline = float("inf")
        finally:
            await self._close_relay(send_close=False)
            worker.stop()
            self._worker = None
            self._set_status(state="scanning", port=None, device_id=None, relay_connected=False)
        return recognized

    def _handle_device_status(self, frame: UsbFrame) -> bool:
        try:
            payload = json.loads(frame.payload)
            device_id = str(payload["device_id"]).strip()
            protocol = int(payload["protocol"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        if not device_id or protocol != 1:
            return False
        self._device_id = device_id
        self._track_sequence(frame.sequence)
        return True

    async def _handle_frame(self, frame: UsbFrame) -> None:
        self._metrics.received_frames += 1
        self._track_sequence(frame.sequence)
        self._write_status()

        if frame.frame_type == UsbFrameType.DEVICE_OPEN:
            await self._open_relay(frame.payload)
        elif frame.frame_type == UsbFrameType.DEVICE_CLOSE:
            await self._close_relay(send_close=True)
        elif frame.frame_type == UsbFrameType.DEVICE_LOG:
            logger.info("device[%s] %s", self._device_id, frame.payload.decode("utf-8", "replace"))
        elif frame.frame_type in {UsbFrameType.DEVICE_TEXT, UsbFrameType.DEVICE_BINARY}:
            if self._relay is None:
                self._send(UsbFrameType.HOST_CLOSE, b"relay is not connected")
                return
            try:
                message: str | bytes
                if frame.frame_type == UsbFrameType.DEVICE_TEXT:
                    message = frame.payload.decode("utf-8")
                else:
                    message = frame.payload
                await self._relay.send(message)
            except (UnicodeDecodeError, ConnectionClosed) as exc:
                logger.warning("device-to-relay forwarding failed: %s", exc)
                await self._close_relay(send_close=True)

    async def _open_relay(self, raw_payload: bytes) -> None:
        try:
            payload: dict[str, Any] = json.loads(raw_payload)
            device_id = str(payload["device_id"]).strip()
            token = str(payload["device_token"]).strip()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self._send(UsbFrameType.HOST_CLOSE, b"invalid device credentials")
            return
        if not device_id or device_id != self._device_id or not token:
            self._send(UsbFrameType.HOST_CLOSE, b"device identity mismatch")
            return

        await self._close_relay(send_close=False)
        try:
            self._relay = await connect(
                self._relay_url,
                additional_headers={
                    "Authorization": f"Bearer {token}",
                    "X-Device-Id": device_id,
                },
                max_size=None,
                ping_interval=20,
                ping_timeout=20,
            )
        except Exception as exc:
            logger.warning("relay connection failed device=%s: %s", device_id, exc)
            self._set_status(last_error=f"relay connection failed: {exc}")
            self._send(UsbFrameType.HOST_CLOSE, b"relay connection failed")
            return

        self._metrics.reconnects += 1
        self._set_status(state="relay_connected", relay_connected=True, last_error=None)
        self._send(UsbFrameType.HOST_OPEN_ACK)
        self._relay_reader = asyncio.create_task(self._read_relay(), name="usb-relay-reader")

    async def _read_relay(self) -> None:
        assert self._relay is not None
        loop = asyncio.get_running_loop()
        audio_deadline = loop.time() - USB_AUDIO_LEAD_SECONDS
        try:
            async for message in self._relay:
                if isinstance(message, str):
                    payload = message.encode("utf-8")
                    if len(payload) > MAX_PAYLOAD_BYTES:
                        raise ValueError("relay text frame exceeds USB protocol maximum")
                    self._send(UsbFrameType.HOST_TEXT, payload)
                else:
                    # Foundry may produce audio faster than wall clock. TCP can
                    # apply backpressure, but USB CDC cannot, so an 8 KiB burst
                    # can overrun the device RX queue while playRaw is waiting.
                    # Prime 100 ms, then pace PCM at its sample rate.
                    now = loop.time()
                    if now - audio_deadline > 0.200:
                        audio_deadline = now - USB_AUDIO_LEAD_SECONDS
                    for offset in range(0, len(message), USB_AUDIO_FRAME_BYTES):
                        chunk = message[offset : offset + USB_AUDIO_FRAME_BYTES]
                        self._send(
                            UsbFrameType.HOST_BINARY,
                            chunk,
                        )
                        audio_deadline += len(chunk) / PCM16_MONO_BYTES_PER_SECOND
                        delay = audio_deadline - loop.time()
                        if delay > 0:
                            await asyncio.sleep(delay)
        except (ConnectionClosed, ValueError) as exc:
            logger.info("relay reader ended device=%s: %s", self._device_id, exc)
        finally:
            self._relay = None
            self._relay_reader = None
            self._set_status(state="ready", relay_connected=False)
            self._send(UsbFrameType.HOST_CLOSE, b"relay disconnected")

    async def _close_relay(self, *, send_close: bool) -> None:
        reader = self._relay_reader
        relay = self._relay
        self._relay_reader = None
        self._relay = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        if relay is not None:
            await relay.close()
        if reader is not None and reader is not asyncio.current_task():
            await asyncio.gather(reader, return_exceptions=True)
        if send_close:
            self._send(UsbFrameType.HOST_CLOSE)
        self._set_status(relay_connected=False)

    def _send(self, frame_type: UsbFrameType, payload: bytes = b"") -> None:
        if self._worker is None or not self._worker.send(frame_type, payload):
            self._set_status(last_error="serial transmit queue is full")
            return
        self._metrics.transmitted_frames += 1
        self._write_status()

    def _track_sequence(self, sequence: int) -> None:
        if (
            self._expected_device_sequence is not None
            and sequence != self._expected_device_sequence
        ):
            gap = (sequence - self._expected_device_sequence) & 0xFFFF
            self._metrics.sequence_gaps += gap or 1
        self._expected_device_sequence = (sequence + 1) & 0xFFFF

    def _handle_noise(self, value: UsbFrame | bytes | str) -> None:
        if not isinstance(value, bytes):
            return
        self._metrics.raw_log_bytes += len(value)
        text = value.decode("utf-8", "replace").strip()
        if text:
            logger.info("device-raw[%s] %s", self._status.port, text)
        self._write_status()

    def _set_status(self, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(self._status, key, value)
        self._write_status()

    def _write_status(self) -> None:
        self._status_writer.write(self._status, self._metrics)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge Stack-chan USB frames to Local Relay")
    parser.add_argument("--relay-url", default="ws://127.0.0.1:8080/v1/realtime")
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(UsbBridge(args.relay_url, args.status_file).run())


if __name__ == "__main__":
    main()
