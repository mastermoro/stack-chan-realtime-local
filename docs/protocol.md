# Stack-chan / Relay protocol v1

Transport: WebSocket. Use `ws://` only on a trusted local LAN during development;
use secure WebSocket (`wss://`) for every Internet-facing Relay.

## Authentication headers

```http
Authorization: Bearer <DEVICE_TOKEN>
X-Device-Id: stackchan-001
```

## Audio

- WebSocket Binary Frame
- PCM16 little endian
- 24,000 Hz
- mono
- recommended device frame: 20 ms / 480 samples / 960 bytes
- Relay-to-device output is split into binary frames of at most 8 KiB so it
  remains within the ESP WebSocket client's receive limit. A frame boundary is
  not an audio-message boundary; play frames in received order.

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
When a WebSocket client has an output-playback position, it should include
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
