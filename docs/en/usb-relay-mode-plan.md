# USB Relay Mode Implementation Plan

[日本語](../usb-relay-mode-plan.md) | [English](usb-relay-mode-plan.md)

Last updated: 2026-08-29

Status: Software implemented (extended physical CoreS3 and failure-recovery gates remain under verification)

This historical document preserves the USB Relay mode design decisions, implementation considerations,
and acceptance conditions. `README.md` is authoritative for current usage instructions, and
`docs/protocol.md` is authoritative for the communication specification.

## Objective

Added a USB path that allows Stack-chan to use Relay on a USB-connected Windows PC while retaining the
Wi-Fi/WebSocket path.

Users can select `AUTO`, `Wi-Fi`, or `USB` from the Settings (Volume adjustment) page on Stack-chan.
USB mode makes conversation available even where Stack-chan cannot connect to Wi-Fi.

## Revalidation results

### Conclusion

The hardware has ample capability, and USB Relay mode is feasible. However, an implementation that writes
PCM directly to the current `Serial` is not acceptable. The following conditions are gates for beginning
the main implementation.

- Build a test implementation on the currently used USB Serial/JTAG (HWCDC) that can detect frame loss.
- Verify at least 2x audio bandwidth headroom, extended stability, disconnect recovery, and non-blocking
  behavior when the host stops, using physical CoreS3 hardware and Windows.
- Separate USB I/O from the main loop so audio, display, and camera processing do not stop.
- Separate Wi-Fi connection state from Relay/Agent state, and independently handle Wi-Fi startup and
  connection after a USB disconnect.
- Guarantee that switching USB/Wi-Fi does not duplicate Foundry sessions for the same Device ID.

### Verified hardware and current environment

| Item | Verified result | Assessment |
| --- | --- | --- |
| SoC | ESP32-S3, dual-core Xtensa LX7, up to 240 MHz | Sufficient for USB framing |
| Memory | CoreS3 has 16 MB Flash and 8 MB PSRAM. SoC internal SRAM is 512 KB | Tens of KB of queues can be added, but internal RAM must be measured |
| USB | USB 2.0 Full-Speed with fixed-function USB Serial/JTAG and USB OTG | Physical bandwidth is sufficient for 48 KB/s audio |
| Current USB settings | `ARDUINO_USB_MODE=1`, `ARDUINO_USB_CDC_ON_BOOT=1` | `Serial` is HWCDC, not a normal UART |
| Current Arduino Core | Arduino-ESP32 2.0.17 series | Physical-device gate required for extended HWCDC transfers |
| Current static usage | Existing ELF uses about 52 KB DRAM data, about 65 KB bss, and about 1.89 MB Firmware | Ample Flash; dynamic internal RAM must be measured separately |

The official CoreS3 specifications state 240 MHz, 16 MB Flash, 8 MB PSRAM, and USB Type-C OTG/CDC support.
The official ESP32-S3 specifications state 512 KB SRAM, Full-Speed USB OTG, and USB Serial/JTAG.

- [Official M5Stack CoreS3 specifications](https://docs.m5stack.com/en/core/CoreS3)
- [Official ESP32-S3 data sheet](https://documentation.espressif.com/esp32_s3_datasheet_en.pdf)
- [Official ESP32-S3 USB Serial/JTAG guide](https://docs.espressif.com/projects/esp-idf/en/release-v5.3/esp32s3/api-guides/usb-serial-jtag-console.html)

### Bandwidth assessment

Current audio is PCM16, 24 kHz, mono, in 20 ms units.

| Content | Value |
| --- | ---: |
| 1 frame | 480 samples / 960 bytes |
| Per 1 second | 50 frames |
| Audio payload | 48,000 bytes/s |
| Estimate including COBS-equivalent encoding, header, and CRC | About 49 KB/s |
| Main-direction bandwidth required for current half-duplex operation | About 49 KB/s |
| Combined bandwidth for future full-duplex operation | About 98 KB/s |

`monitor_speed = 115200` and `Serial.begin(115200)` are not USB CDC line-speed limits. USB Serial/JTAG
connects directly to the PC as fixed-function USB CDC-ACM, so the previous assessment that
"115200 bps is insufficient" is withdrawn.

However, USB Serial/JTAG has small internal buffers and can pause briefly under host backpressure.
The official Arduino-ESP32 repository also contains loss reports and fixes for ESP32-S3 HWCDC, so
feasibility must not be decided from theoretical bandwidth alone.

- [USB Serial/JTAG buffer constraints](https://docs.espressif.com/projects/esp-idf/en/release-v5.3/esp32s3/api-guides/usb-serial-jtag-console.html#data-buffering)
- [Arduino-ESP32 HWCDC loss report](https://github.com/espressif/arduino-esp32/issues/9378)

### USB approach selection

The first choice is to retain the currently enabled fixed-function USB Serial/JTAG (HWCDC). It handles
logs, communication, and automatic flashing with `esptool.py` through the same COM port, minimizing the
impact on current operations.

Switching to USB OTG/TinyUSB CDC is not the first choice. It cannot be used simultaneously with
fixed-function USB Serial/JTAG and affects USB device re-enumeration after application startup, VID/PID
or COM-number changes, and automatic flashing procedures. Evaluate it as a second choice only if HWCDC
cannot pass the performance gates.

### Feasibility decision

| Area | Decision | Condition |
| --- | --- | --- |
| USB physical bandwidth | Feasible | Verify at least 2x headroom on physical hardware |
| CPU performance | Feasible | Separate USB I/O and encoding into a dedicated task |
| Flash/PSRAM | Feasible | Ample headroom based on current usage |
| Internal RAM | Conditionally feasible | Measure heap with Wi-Fi, camera, and USB used together |
| Windows bridge | Feasible | Can be implemented with `pyserial` and WebSocket |
| Retaining current Wi-Fi | Conditionally feasible | Wi-Fi state and Agent state must be separated |
| ESP-NOW | Feasible | Explicitly stop it while USB is the active path |
| Automatic flashing | Feasible | Manager must own COM-port arbitration |

### Baseline check on connected physical hardware

On 2026-08-29, USB data-path enumeration and current logs were checked with a CoreS3 connected to Windows.

| Item | Measured result |
| --- | --- |
| COM port | `COM3` |
| USB ID | `VID_303A&PID_1001&MI_00` |
| Windows driver | Microsoft `usbser.inf`, state Started |
| Port open | Succeeded while specifying 115200. As this is HWCDC, the value does not limit physical speed |
| Log reception for 8 seconds | 1,797 bytes |
| Physical-device log | Confirmed PSRAM enabled, Wi-Fi connected, and ESP-NOW channel 9 |

`USB_UART_CHIP_RESET` and a Boot ROM log were observed 1 time during the first port open. They did not recur
on the 2nd port open, so the port open was not established as the cause. However, the acceptance
conditions include bridge recovery if the device restarts and re-enumerates once during initial enumeration
or Manager startup.

Because the current firmware did not yet contain a USB audio transport, continuous PCM performance could
not be measured in this check. The performance gates are run after creating test firmware and a bridge.

## Existing specifications to retain

- When connected by Wi-Fi, connect to Relay's `/v1/realtime` by WebSocket as before.
- Do not change the external specifications for the Relay-to-Foundry protocol, device authentication,
  conversation state, Web search, or local Functions.
- Retain PCM16 little endian, 24 kHz, mono audio.
- Retain the existing protocol semantics for JSON control messages and audio frames.
- Do not regress current behavior in fixed Wi-Fi mode.

## Finalized requirements

### Mode selection

Add a 3-state `AUTO`, `Wi-Fi`, and `USB` switch to the Settings (Volume adjustment) page. Display the
selected value separately from the communication path actually in use.

Persist the selected value in nonvolatile device storage across restarts. If no value is saved or the saved
value is invalid, default to `AUTO`.

| Selected mode | Relay path | Wi-Fi behavior | When the path disconnects |
| --- | --- | --- | --- |
| `AUTO` | Prefer USB | Stopped while USB is in use | Start Wi-Fi after USB disconnect and recover |
| `Wi-Fi` | Fixed to Wi-Fi | Continue connection and reconnection | Wait for Wi-Fi recovery; do not fall back to USB |
| `USB` | Fixed to USB | Stop connection and reconnection | Wait for USB recovery; do not fall back to Wi-Fi |

### AUTO mode

- At startup, wait up to 3 seconds for a handshake with the USB bridge.
- If it does not complete within 3 seconds, use Wi-Fi as the active path and start a Relay session.
- Do not start Wi-Fi during those 3 seconds; begin Wi-Fi connection only after USB has failed to establish.
- If USB becomes available during a Wi-Fi conversation, defer switching until the idle state after the
  conversation ends.
- Before switching to USB, end the old Wi-Fi Relay connection and ESP-NOW, then stop Wi-Fi.
- If USB is the active path and the USB connection is lost, stop the current conversation, start Wi-Fi,
  and begin a new Relay session after connection completes.
- Recovery from USB disconnect to Wi-Fi can take several seconds or more for Wi-Fi association and Relay
  connection.

### Manual switching and sessions

- Begin a manual switch immediately after selection.
- Stop recording and playback, and safely close the current Relay connection.
- Start a new Relay/Foundry session on the selected path.
- Do not carry over conversation context from before the switch.
- If the cable is unplugged while fixed to `USB`, wait for USB reconnection.
- If the connection is lost while fixed to `Wi-Fi`, wait for Wi-Fi reconnection.

### USB mode

- Make conversation available even when Stack-chan cannot connect to Wi-Fi.
- Initially support 1 USB-connected Stack-chan per 1 Windows PC.
- Add no new limitation to existing simultaneous connections for Wi-Fi devices.
- ESP-NOW remote neck control is out of scope while USB is the actual Relay path.
- Determine USB availability from a completed handshake with the PC bridge, not from VBUS alone.

### Windows USB bridge

- Starting Relay in Relay Manager also starts the USB bridge automatically.
- Automatically detect the Stack-chan COM port and follow hot-plug and COM-number changes.
- If multiple candidates cannot be uniquely identified, do not connect and show the reason in Manager.
- Forward device communication over USB to `ws://127.0.0.1:8080/v1/realtime`.
- Retain authentication with the existing Device ID and Device Token, and do not log credentials.
- Stop the bridge when Relay stops.

### Logging and COM-port management

- Multiplex USB communication, device logs, and control messages as distinguishable frames.
- Separate device logs during USB communication in the bridge and make them viewable in Relay Manager.
- When firmware flashing or a direct serial monitor begins, pause the bridge and release the COM port.
- After flashing or monitoring ends, redetect the device and automatically restore the bridge.

## Impact analysis for current Wi-Fi specifications

### Current tight coupling

The current implementation always runs `WiFi.mode(WIFI_STA)` and `WiFi.begin()` at startup, then starts
ESP-NOW and the Relay WebSocket together after Wi-Fi connects. Reaching the Wi-Fi retry limit also changes
the Agent state itself to `Error`. This structure cannot safely express starting Wi-Fi after a USB disconnect
or switching from fixed USB to AUTO/Wi-Fi.

Separate these states:

- User selection: `AUTO`, `Wi-Fi`, `USB`
- Actual Relay path: `None`, `Wi-Fi`, `USB`
- Wi-Fi link: `Off`, `Connecting`, `Connected`, `Backoff`, `Failed`
- USB link: `Detached`, `Enumerated`, `Handshaking`, `Ready`
- Relay/Agent state: The current range from `Disconnected` through `Error`
- Pending switch: `None`, `SwitchToUsbWhenReady`

While USB is the active path in AUTO, keep the Wi-Fi link `Off`. On USB disconnect, transition Relay/Agent
to the equivalent of `Connecting`, then start Wi-Fi. In fixed Wi-Fi mode, retain current retry intervals and
failure display. When moving from fixed USB to AUTO or Wi-Fi, reset the Wi-Fi retry count and failure latch.

### ESP-NOW

Current ESP-NOW is initialized on the AP's channel after connecting to the Wi-Fi AP. Retain this order on
the Wi-Fi path. Before switching to USB, complete `esp_now_deinit()`, discard received neck controls, and
then stop Wi-Fi. Do not start ESP-NOW while USB is the active path.

When returning to Wi-Fi, reinitialize ESP-NOW after STA connection completes and the channel is known.
Do not change the existing requirement that ESP-NOW use the current Wi-Fi channel.

- [Official ESP-NOW guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/network/esp_now.html)
- [Official ESP32-S3 Wi-Fi guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/api-guides/wifi-driver/overview.html)

### Conversation state and switch timing

Current code automatically returns to the next Listening state after `response.done`, so merely waiting for
`Ready` loses the opportunity to switch to USB. When a USB switch is pending, perform it during the Ready
transition after `response.done`, before sending the next `audio.start`.

For a manual switch, USB removal, or Wi-Fi disconnect, preserve this order:

1. Stop new recording.
2. Stop playback and discard audio send/receive queues.
3. Close the current Relay connection.
4. Stop or start ESP-NOW and Wi-Fi as needed.
5. Handshake the new path and start a new Relay session.

### Duplicate Relay sessions

Relay currently does not make multiple WebSockets from the same Device ID mutually exclusive. If old TCP
disconnect detection is delayed during a path switch, Foundry sessions can be duplicated temporarily. The
device must always close the old path before starting the new one, and Relay must add a connection generation
or exclusion mechanism per Device ID. When a new connection is accepted, end the old connection without
affecting simultaneous connections from different Device IDs.

## Implementation architecture

```text
Stack-chan
  ├─ WiFiWebSocketTransport ────────┐
  └─ UsbSerialTransport             │
             │                      │
             v                      │
      Windows USB Bridge            │
             │ WebSocket            │
             └──────────────────────┤
                                    v
                       Local Relay /v1/realtime
                                    │
                                    v
                            Microsoft Foundry
```

Separate transport-specific processing from Stack-chan's Relay client. Upper layers use only the existing
connection notifications and JSON/PCM send/receive interfaces, without awareness of the selected transport.

Provide at least these USB frame types:

- Handshake and connection state
- JSON control messages
- PCM binary audio
- Heartbeat
- Diagnostic logs
- Error notifications

Use a zero-delimited scheme such as COBS as the first candidate so framing can resynchronize. Include
Version, Type, Sequence, Payload Length, Payload, and CRC32. Sequence and CRC make loss or corruption from
HWCDC or host processing observable.

Control frames support acknowledgement and retransmission. Real-time audio is not retransmitted because that
would increase latency; discard corrupt frames and count losses. Boot ROM and Framework logs before USB
connection can appear as out-of-frame data, so the bridge skips input and resynchronizes until delimiter and
CRC match.

Replace the existing 25 `Serial.print*` calls with a common Logger. After USB handshake, send them as log
frames at lower priority than the audio queue. Finalize the format after CoreS3 bandwidth and CPU-load
verification.

## Implementation phases (history)

The following is the checklist from the start of implementation and does not indicate unfinished work.
Physical-device performance gates and acceptance testing remain environment-dependent ongoing checks.

### 1. USB technical validation

- Measure effective bandwidth for stable continuous PCM transmission in both directions on current USB
  Serial/JTAG (HWCDC).
- Measure actual USB throughput and latency rather than the value in `Serial.begin(115200)`.
- Expand TX/RX ring buffers before `begin()` and compare 4 KB, 8 KB, and 16 KB.
- Put USB read/write and framing in a dedicated FreeRTOS task connected to the main loop by queues.
- Validate under 960 byte/20 ms real traffic plus control and logging.
- Verify re-enumeration after USB removal, PC restart, and Stack-chan restart.
- Keep display, touch, audio, and watchdog operating when the PC bridge stops reading.
- Verify COM-port behavior during PlatformIO flashing.
- Measure peak internal heap with camera and USB, and during short transition periods where Wi-Fi and USB
  are both enabled for path switching.
- If the following performance gates cannot pass, do not proceed to the main implementation; evaluate an
  Arduino Core update or TinyUSB.

Performance gates:

- Transfer at least 100 KB/s in each direction continuously for 30 minutes and record CRC mismatches,
  Sequence losses, and short writes.
- Transfer 48 KB/s simultaneously in each direction for 30 minutes with 0 corrupted frame boundaries.
- Under realistic audio, keep added latency from USB reception to bridge WebSocket transmission at
  p99 20 ms or less.
- Keep audio queue residency normally at 40 ms or less and at 100 ms or less even during faults.
- Detect and handle short writes in the USB task without stopping the main loop continuously for 20 ms or more.
- Leave at least 64 KB free internal heap and a 32 KB largest contiguous block while using camera and USB,
  and during Wi-Fi/USB switching transitions.
- Reconnect without manual reset across 100 USB disconnect/reconnect cycles and 20 PC sleep/resume cycles.
- If the device restarts on the first COM open after initial enumeration, re-enumerate and connect automatically.

### 2. Specify protocol and state transitions

- Define USB frame format, maximum length, timeout, heartbeat, and resynchronization.
- Create a transition table for `AUTO`, `Wi-Fi`, and `USB`.
- Define user selection, actual Relay path, Wi-Fi link, USB link, and Agent state separately.
- Define switching behavior in Ready, Listening, Thinking, Searching, and Speaking.
- Define recording stop, playback stop, and session-end order for disconnects and manual switching.
- Define control-frame retransmission and audio-frame discard/loss measurement policies.

### 3. Stack-chan firmware

- Separate Relay communication behind a transport-independent interface.
- Retain the existing WebSocket implementation as the equivalent of `WiFiWebSocketTransport`.
- Implement USB framing, Sequence/CRC, send/receive queues, and handshake.
- Add a dedicated USB I/O task; do not perform blocking writes in the main loop.
- Bound and prioritize USB TX/RX queues in the order audio, control, and logs.
- Implement mode control and the fallback state machine.
- Separate Wi-Fi failure state from Relay/Agent state.
- Add explicit `end()` to Transport and start the new path only after the old path disconnects.
- Add the 3-state switch, selected value, actual path, and pending/error state to Settings.
- Persist the selected value.
- Deinitialize ESP-NOW and discard received controls while USB is actually connected.
- Replace `Serial.print*` with a common Logger that can switch to USB frames.

Primary change candidates:

- `stackchan/include/relay_client.hpp`
- `stackchan/src/relay_client.cpp`
- `stackchan/src/main.cpp`
- New USB transport header and implementation

### 4. Windows USB bridge

- Add `pyserial` as a direct dependency and implement COM-port detection and Stack-chan handshake.
- Do not identify a device by VID/PID alone; use the Protocol Version and Device ID response on candidates.
- Convert USB frames and WebSocket Text/Binary Frames bidirectionally.
- Set Device ID and Device Token in existing Relay authentication headers.
- Manage USB and WebSocket disconnect/reconnect independently.
- Bound audio backpressure and queues to prevent unbounded latency accumulation.
- Separate log frames from communication frames.
- Measure CRC mismatches, Sequence losses, short reads/writes, queue residence, and reconnect count.
- Close the corresponding Relay WebSocket immediately on COM disconnect.
- Add connection-generation management per Device ID to the Relay gateway and end the remaining old
  connection when a new one arrives.

### 5. Relay Manager integration

- Start the USB bridge after Relay readiness is confirmed, and stop the bridge before Relay.
- Display detected COM, Device ID, handshake, and WebSocket connection state.
- Display USB device logs and bridge logs.
- Distinguish port conflicts, multiple candidates, authentication failure, and Relay not running.
- Monitor abnormal bridge exit and restart it automatically where safe.

Primary change candidates:

- `relay/local_manager.py`
- New Python module for the USB bridge
- Tests under `relay/tests/`

### 6. Flashing and monitor integration

- Allow `deploy-from-relay.ps1` to request COM release from Manager.
- Start redetection after flashing, whether it succeeds or fails.
- Add Manager actions to pause and resume the bridge for direct serial monitoring.
- Do not forcibly take the COM port; fail safely with a reason when it is in use.

### 7. Testing and documentation

- Unit-test the state machine, USB frames, authentication, and log separation.
- Add PC integration tests using simulated serial and a local WebSocket.
- Test that Wi-Fi initializes after USB disconnect and correctly reflects connection success/failure in
  Agent state.
- Test that a new connection with the same Device ID reliably ends the old Relay session.
- Inject stopped host reading, short writes, CRC corruption, and mid-stream disconnects.
- Test extended audio and failure recovery on physical CoreS3 hardware.
- Run existing tests and physical regression checks for fixed Wi-Fi mode.
- Update README, development procedures, protocol, and troubleshooting documentation.

## Acceptance tests

| Case | Expected result |
| --- | --- |
| Start in AUTO with USB connected | Select USB within 3 seconds and do not create a Wi-Fi Relay session |
| Start in AUTO without USB | Connect over Wi-Fi as before |
| Connect USB during an AUTO Wi-Fi conversation | Maintain the conversation and switch to USB when idle |
| Unplug the cable during an AUTO USB conversation | Stop the conversation, start Wi-Fi, and recover after connection |
| Unplug the cable while fixed to USB | Wait for USB reconnection without switching to Wi-Fi |
| Connect USB while fixed to Wi-Fi | Keep Wi-Fi without switching to USB |
| Fixed USB with no Wi-Fi/AP | Converse over USB |
| AUTO while USB is in use | Wi-Fi and ESP-NOW are stopped |
| Wi-Fi connection fails after USB disconnect | Show an error after defined retries and continue monitoring USB reconnection |
| Switch from USB to Wi-Fi | Reinitialize ESP-NOW on the channel after STA connection |
| Change mode manually | End the current session and connect with a new session |
| Old TCP disconnect detection is delayed during switching | Do not duplicate Foundry sessions for the same Device ID |
| Restart Stack-chan | Restore the previously selected mode |
| Restart Relay Manager | Relay, bridge, and USB device connection recover automatically |
| Flash firmware | Release COM for flashing and restore the bridge afterward |
| Emit logs during USB communication | View logs in Manager without corrupting audio |
| PC bridge temporarily stops reading | Do not stop the main loop; recover or disconnect within bounds |
| Device restarts on first COM open | Bridge connects automatically after re-enumeration |
| Extended conversation in fixed Wi-Fi | Operate equivalently to before USB was added |

## Risks and mitigations

### Effective USB bandwidth

`115200` is not an HWCDC physical-bandwidth limit, and physical bandwidth has headroom. However,
fixed-function USB Serial/JTAG and Arduino HWCDC use small FIFO/ring buffers; mishandling a host read stall
or short write causes loss or blocks the main loop. Implement a dedicated task, expanded buffers, and
Sequence/CRC measurement, and pass the performance gates.

### Pinned Arduino Core version

The project is currently pinned to the Arduino-ESP32 2.0.17 series. Because the official repository contains
HWCDC loss fixes, if the pinned version cannot pass the performance gates, evaluate a Core update on a
separate branch instead of hiding it with ad hoc retransmission. Regression-test M5Unified, StackChan-BSP,
WebSockets, camera, audio, and flashing when updating Core.

### Exclusive COM-port use

The bridge, PlatformIO, and serial monitor cannot use the same COM port simultaneously. Make Manager the
owner and implement explicit release and reacquisition procedures.

### Residual audio during switching

To prevent an old path's receive queue from playing in a new session, stop and discard recording, playback,
and send/receive queues in order when switching begins.

### Audio delay from logging

Assign priorities to audio and logs, with audio first. Bound the log queue and discard old diagnostic logs
under overload to protect conversation quality.

### Internal RAM and task contention

Although 8 MB PSRAM has headroom, the USB driver, Wi-Fi, and parts of FreeRTOS queues use internal RAM.
Measure free heap and the largest contiguous block during camera inference, display drawing, audio, USB,
and Wi-Fi/USB switching transitions. Keep USB internal RAM within 32 KB in principle, and reject a
configuration that violates the performance-gate minimums.

### On-demand Wi-Fi recovery

Wi-Fi is stopped while USB is the active AUTO path, avoiding CPU/RF load from Wi-Fi retries during USB
conversation. However, after USB disconnect, Wi-Fi initialization, AP association, WebSocket, and Foundry
connection run in sequence, so recovery is not immediate. Display the recovery phase and set timeouts for
each phase.

Continue monitoring for USB reconnection even if Wi-Fi connection fails. If USB recovers first, stop the
Wi-Fi connection attempt and resume with USB as the active path.

### Duplicate Relay sessions

If the old WebSocket remains during a path switch, multiple Foundry sessions can be created for the same
device. Do not depend only on device-side close ordering; in the Relay gateway, keep only the latest
connection active per Device ID.

### Regression of existing Wi-Fi behavior

Do not remove or replace the Wi-Fi implementation; retain it as one implementation of the common interface.
Include automated and physical tests of fixed Wi-Fi mode in the completion conditions.

## Completion conditions

- All acceptance tests pass.
- Continuous conversation over USB without Wi-Fi operates stably.
- All USB performance gates pass and measurements are documented.
- Recovery after USB removal, PC restart, and device restart follows the specification.
- Existing functionality does not regress in fixed Wi-Fi mode.
- Wi-Fi/ESP-NOW stop while USB is used in AUTO, and Wi-Fi starts on demand after USB disconnect.
- A path switch does not duplicate Foundry sessions for the same Device ID.
- Secrets such as Device Token are not logged.
- Setup, mode switching, flashing, and failure-response procedures are documented.

## Implementation and physical-device verification results (2026-08-29)

The basic implementation in this plan is complete. CoreS3 firmware builds with internal RAM
116,964 / 327,680 bytes (35.7%) and Flash 1,895,933 / 6,553,600 bytes (28.9%), and was flashed to the
physical device through ESP32-S3 USB Serial/JTAG on COM3.

In a physical AUTO test, stopping the USB path caused Stack-chan to connect from its Wi-Fi address to the
Relay WebSocket. Restoring the USB bridge closed the old Wi-Fi session and returned to the in-PC USB path.
Measurements after USB recovery showed 0 CRC/frame corruptions and 0 Sequence losses. An immediate bridge
restart also automatically established a new USB session from synchronization `HOST_CLOSE`.

60 Python tests passed (the 2 existing tests that assume local `.env` is unset were excluded because the
real environment is configured), as did Ruff and `git diff --check`. Coverage includes the USB codec, split
reads, resynchronization after CRC corruption, interleaved logs, Device ID exclusion, and Manager Supervisor.

Extended continuous audio, 100 USB disconnect/reconnect cycles, 20 PC sleep cycles, and free-heap measurement
with simultaneous camera use remain durability test items. They do not prevent basic functional feasibility,
but the Go/No-Go criteria in this document apply to the release decision.
