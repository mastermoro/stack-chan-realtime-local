[日本語](../protocol.md) | **English**

# Stack-chan / Relay protocol v1

The protocol carries JSON control messages and binary PCM audio over either a
Wi-Fi WebSocket or the USB bridge envelope described below. Use `ws://` only on
a trusted local LAN; use secure WebSocket (`wss://`) for every Internet-facing
Relay.

## Authentication headers

```http
Authorization: Bearer <DEVICE_TOKEN>
X-Device-Id: stackchan-001
```

## Audio

- Binary message (a WebSocket binary frame or USB `*_BINARY` payload)
- PCM16 little endian
- 24,000 Hz
- mono
- recommended device frame: 20 ms / 480 samples / 960 bytes
- Relay-to-device output is split into binary messages of at most 8 KiB. This
  remains within both the ESP WebSocket client and USB envelope limits. A
  message boundary is not an audio boundary; play messages in received order.

## Device -> Relay JSON

### hello

Must be the first text frame after connection.

```json
{
  "type": "hello",
  "protocol": 1,
  "device_id": "stackchan-001",
  "audio": {
    "format": "pcm16",
    "sample_rate": 24000,
    "channels": 1
  }
}
```

### UI/turn controls

```json
{"type":"audio.start"}
{"type":"audio.stop"}
{"type":"response.cancel"}
{"type":"conversation.pause","item_id":"item_123","content_index":0,"audio_end_ms":1500}
{"type":"ui.mode","mode":"face"}
{"type":"ping"}
```

Server VAD is authoritative in v0.1. The Relay accepts audio frames only while
the device state is `listening`; clients must stop their microphone while the
Relay is thinking, searching, or speaking. `audio.start` / `audio.stop` control
the client conversation state and are retained for future manual-VAD modes.
`conversation.pause` stops the current turn while keeping the Realtime session
and its conversation context available for a later `audio.start`.
When a client has an output-playback position, it should include
`item_id`, `content_index`, and `audio_end_ms`. The Relay cancels the active
response and truncates the unheard audio from the conversation history. These
three fields are optional for simple embedded clients.

`ui.mode` accepts `standard` or `face`. The Relay updates the Realtime system
instructions so Face mode uses the cat-like speaking style without resetting
the conversation context.

## Relay -> Device JSON

```json
{"type":"session.ready","device_id":"stackchan-001"}
{"type":"session.reconnected","device_id":"stackchan-001"}
{"type":"state","state":"searching"}
{"type":"response.done"}
{"type":"output_audio.started","item_id":"item_123","content_index":0}
{"type":"emotion","emotion":"happy"}
{"type":"notice","title":"LOCAL ACTION","detail":"Browser opened on PC"}
{"type":"pong"}
```

`emotion` is advisory UI state. Supported values are `neutral`, `happy`, `sad`,
`angry`, `surprised`, and `sleepy`; clients must ignore unknown values.

When Web Search is used:

```json
{
  "type": "sources",
  "sources": [
    {"title":"Microsoft Learn","url":"https://learn.microsoft.com/..."}
  ]
}
```

## USB bridge envelope

USB mode carries the same JSON text and PCM binary messages through the PC bridge. Each serial packet is COBS encoded and surrounded by zero delimiters. A leading delimiter discards boot/framework log bytes before a frame. The decoded little-endian layout is:

```text
uint8  envelope_version = 1
uint8  frame_type
uint16 sequence
uint32 payload_length
byte   payload[payload_length]   // maximum 8192 bytes
uint32 crc32                     // header + payload
```

Frame types are `HOST_PROBE=0x01`, `DEVICE_STATUS=0x02`, `DEVICE_OPEN=0x03`, `HOST_OPEN_ACK=0x04`, `DEVICE_CLOSE=0x05`, `HOST_CLOSE=0x06`, `DEVICE_TEXT=0x10`, `DEVICE_BINARY=0x11`, `HOST_TEXT=0x20`, `HOST_BINARY=0x21`, `DEVICE_LOG=0x30`, and `ERROR=0x7f`.

The host sends `HOST_PROBE` every 500 ms. The device replies with `DEVICE_STATUS` containing `{"protocol":1,"device_id":"..."}` whether or not USB is the selected Relay route. To open USB as the route, the device sends `DEVICE_OPEN` with its device ID and token; the bridge then opens the authenticated local WebSocket and returns `HOST_OPEN_ACK`. Thereafter `*_TEXT` and `*_BINARY` preserve the WebSocket message types. Sequence gaps and CRC failures are diagnostic counters; a damaged packet is discarded without terminating the stream.

## Playback interruption fence (issue #12)

Updated firmware adds a monotonically increasing `interrupt_id` (integer 1–4294967295)
to `conversation.pause` / `response.cancel`. It stops playback and suppresses response
state, PCM, and completion callbacks locally before sending. Face-mode microphone
capture can resume immediately.

```json
{"type":"conversation.pause","interrupt_id":42}
{"type":"conversation.paused","interrupt_id":42}
```

The Relay drains in-flight output before sending the matching acknowledgment on the
same ordered stream. No output from the cancelled response may follow that fence.
The USB bridge wakes its pacing wait and discards queued output through the matching
fence. Only the latest matching ID reopens output; stale acknowledgments, `speaking`,
timers, and buffered `session.reconnected` messages do not. A genuinely new transport
connection resets the local fence.

Clients without IDs remain supported. Update Relay / USB bridge before flashing the
new firmware. An old Relay cannot acknowledge the fence, so updated firmware fails
closed and reconnects after its acknowledgment deadline instead of replaying cancelled speech.
Recovery requires the updated bridge too; it does not silently downgrade to unsafe playback.

If an upstream no-active-response cancellation error omits its client event ID while
cancellation is pending, the Relay reconnects Foundry instead of guessing which
cancel it acknowledges. This exceptional recovery resets conversation context, but
prevents stale playback from reopening or leaving output permanently muted.

A missing acknowledgment after five seconds disconnects the old transport; elapsed time
never releases playback by itself. USB recovery adds a positive uint32 `recovery_id` to
DeviceOpen. Only after closing the old WebSocket and opening a fresh one does the bridge
echo this ID in the JSON HostOpenAck payload. While recovering, firmware rejects empty,
nonmatching, or incorrectly typed open acknowledgments. Ordinary legacy DeviceOpen and
empty HostOpenAck remain supported. Failed control-frame enqueue closes the relay socket.

A HostClose received while a USB fence is pending also requires the correlated recovery
handshake. A buffered HostClose followed by an empty HostOpenAck is insufficient to reopen output.
