"""In-process RelaySession -> paced UsbBridge -> serial sink interruption tests."""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.session.orchestrator import RelaySession
from app.usb.bridge import UsbBridge
from app.usb.protocol import UsbFrame, UsbFrameType


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_count", [1, 2])
async def test_relay_fence_crosses_paced_bridge_before_next_turn(interrupt_count):
    relay_output = asyncio.Queue()
    upstream_events = asyncio.Queue()
    serial_frames = []
    audio_started = asyncio.Event()
    fresh_audio_sent = asyncio.Event()
    fresh_audio = b"\x00\x11" * 100
    cancellations = []

    class DeviceSocket:
        async def send_json(self, payload):
            relay_output.put_nowait(json.dumps(payload))

        async def send_bytes(self, payload):
            relay_output.put_nowait(payload)

    class Upstream:
        async def cancel(self, **kwargs):
            cancellations.append(kwargs)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await upstream_events.get()

    upstream = Upstream()
    upstream.response = upstream
    session = RelaySession(DeviceSocket(), "test-device", Settings())

    class BridgeSocket:
        async def send(self, message):
            await session._handle_device_control(upstream, message)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await relay_output.get()

    def serial_send(kind, payload=b""):
        serial_frames.append((kind, payload))
        if kind == UsbFrameType.HOST_BINARY:
            audio_started.set()
            if payload == fresh_audio:
                fresh_audio_sent.set()
        return True

    bridge = UsbBridge("ws://unused")
    bridge._relay = BridgeSocket()
    bridge._worker = SimpleNamespace(send=serial_send)
    upstream_reader = asyncio.create_task(session._realtime_to_device(upstream))
    bridge_reader = asyncio.create_task(bridge._read_relay())

    def emit_created(response_id):
        upstream_events.put_nowait(SimpleNamespace(
            type="response.created", response=SimpleNamespace(id=response_id),
        ))

    def emit_audio(response_id, data):
        upstream_events.put_nowait(SimpleNamespace(
            type="response.output_audio.delta", response_id=response_id,
            delta=base64.b64encode(data).decode(),
        ))

    async def control(payload, sequence):
        await bridge._handle_frame(UsbFrame(
            UsbFrameType.DEVICE_TEXT, sequence, json.dumps(payload).encode(),
        ))

    try:
        emit_created("old")
        emit_audio("old", b"\xaa\xbb" * 4096)
        await asyncio.wait_for(audio_started.wait(), timeout=1)
        at_interrupt = len(serial_frames)
        for interrupt_id in range(1, interrupt_count + 1):
            await control(
                {"type": "conversation.pause", "interrupt_id": interrupt_id}, interrupt_id
            )
        await control({"type": "audio.start"}, interrupt_count + 1)
        emit_audio("old", b"\xaa\xbb" * 20)
        upstream_events.put_nowait(SimpleNamespace(
            type="response.done", response=SimpleNamespace(id="old"),
        ))
        emit_created("new")
        emit_audio("new", fresh_audio)
        await asyncio.wait_for(fresh_audio_sent.wait(), timeout=1)

        after_interrupt = serial_frames[at_interrupt:]
        assert [data for kind, data in after_interrupt if kind == UsbFrameType.HOST_BINARY] == [
            fresh_audio
        ]
        controls = [json.loads(data) for kind, data in after_interrupt
                    if kind == UsbFrameType.HOST_TEXT]
        # The first acknowledgment may arrive before the second tap is issued.
        # Only acknowledgments still pending when superseded must be discarded.
        fence_ids = [item["interrupt_id"] for item in controls
                     if item["type"] == "conversation.paused"]
        assert fence_ids
        assert fence_ids == list(range(fence_ids[0], interrupt_count + 1))
        assert controls[0]["type"] == "conversation.paused"
        latest_fence = json.dumps({
            "type": "conversation.paused", "interrupt_id": interrupt_count,
        }).encode()
        assert after_interrupt.index((UsbFrameType.HOST_TEXT, latest_fence)) < (
            after_interrupt.index((UsbFrameType.HOST_BINARY, fresh_audio))
        )
        assert {"type": "state", "state": "listening"} in controls
        assert {"type": "response.done"} not in controls
        assert {"type": "state", "state": "ready"} not in controls
        assert len(cancellations) == interrupt_count
        assert bridge._pending_interrupt is None
    finally:
        upstream_reader.cancel()
        bridge_reader.cancel()
        await asyncio.gather(upstream_reader, bridge_reader, return_exceptions=True)
