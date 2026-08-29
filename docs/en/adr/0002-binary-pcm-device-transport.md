[日本語](../../adr/0002-binary-pcm-device-transport.md) | **English**

# ADR-0002: Send raw PCM as WebSocket binary frames between device and Relay

Status: Accepted

Amended by the USB Relay mode: Wi-Fi still uses WebSocket binary frames. The
USB route preserves the same raw PCM binary-message semantics inside its serial
envelope, with the Windows bridge translating to the Relay WebSocket.

## Decision

Stack-chan sends PCM16 24 kHz mono as WebSocket binary frames. JSON is used only for control messages. Base64 conversion is performed by the Relay only when talking to the Realtime API.

## Consequences

- Less bandwidth and CPU overhead on ESP32/CoreS3.
- The device protocol stays independent from Foundry-specific event schemas.
- Relay owns translation between binary device transport and Realtime JSON/base64 events.
