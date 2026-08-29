# Implementation and Operations Lessons (Local Relay / Stack-chan)

[日本語](../implementation-lessons.md) | [English](implementation-lessons.md)

This document records, in chronological order, events that occurred while integrating the local Relay,
Foundry Realtime, headset testing, and Stack-chan firmware. Each entry is intended to preserve not only
the symptom, but also the cause, the response taken, and the design decisions that should be followed in
the future.

## Prerequisites

- The device and Relay use PCM16 / 24 kHz / mono binary audio. The Wi-Fi path carries WebSocket Binary
  Frames, while the USB path carries the same JSON and PCM in USB envelopes with COBS/CRC.
- Foundry Realtime uses `gpt-realtime-2.1`, and Web search uses the Responses API with
  `gpt-5.6-terra`.
- Stack-chan assumes a Relay on the local LAN. The default is `ws://`; TLS is enabled only when the
  Relay is exposed to the Internet.
- Because acoustic echo cancellation (AEC) is unavailable, the current standard operation is half-duplex.

## Chronology

### 1. Added a local startup and management path

Relay start, stop, status, and log viewing were made available through the management UI, and headset
tests could be launched from the same UI. The management UI supervises the Relay process and the
headset test process separately.

**Lessons**

- Determine status from the `/healthz` response, not merely whether a port is open.
- When the management UI stops, also stop the Relay and headset test under its management.
- On Windows, atomically renaming state and control files that are read at short intervals can fail.
  Treat state and control writes as best effort, and do not end an audio session when one fails.

### 2. Added conversation start and pause to the headset test

A simple "start/stop test" also discarded the Foundry session context, so the conversation can now be
started and paused while keeping the test process alive.

**Specification changes**

- `audio.start`: Starts a new listening turn.
- `conversation.pause`: Stops the current turn, but preserves the session while the connection remains alive.
- Pausing in the management UI does not stop the process.

### 3. Speech detection ended too early

Listening completed during short natural pauses, causing the response to begin too soon.

**Cause**

The server VAD end-of-speech decision was too aggressive for conversation.

**Response and current specification**

- The default is now `semantic_vad` with `low` eagerness.
- `server_vad` remains configurable as a fallback for deployment compatibility.
- `interrupt_response` remains `false`. Enabling full-duplex automatic interruption on Stack-chan
  without AEC would misdetect the device's own playback as user speech.

### 4. Input audio was delayed and stale speech was sent

`audio input queue full; dropping frame` appeared, and stale audio could reach Foundry after the user
had finished speaking.

**Causes**

- Work could be queued without limit from the PortAudio callback to the event loop.
- While reading the device WebSocket, Relay directly awaited Foundry's `input_audio_buffer.append`.

**Response and current specification**

- The microphone retains at most 3 20 ms frames and drops the oldest frame under congestion.
- The PortAudio callback coalesces pending work into 1 latest frame.
- Relay also has an input queue of at most 5 frames and separates device WebSocket reception from
  Foundry transmission into different tasks.

**Design decision**

Input audio prioritizes freshness over complete retention. Old microphone audio has little conversational
value, and retaining it increasingly disrupts the next turn.

### 5. Output audio was dropped when the queue was full

`audio output queue full` caused parts of assistant responses to be omitted.

**Response and current specification**

- During normal responses, the output queue is unbounded and output frames are not dropped.
- Only a warning is emitted when the backlog becomes large.
- Listening for the next turn begins only after playback completes.

**Exception**

Unplayed output is discarded only when the user explicitly pauses or interrupts. This is not an omission;
it is an explicit stop request.

### 6. Playback continued after pausing

Even after `response.cancel` was sent to Relay, the device retained already received PCM in its playback
queue, so audio continued to the end.

**Cause**

With WebSockets, the server does not know the device's actual playback position. Stopping output and
truncating conversation history are client responsibilities.

**Response and specification changes**

- The headset test immediately clears its unplayed queue when paused.
- It sends Relay `conversation.pause` and the played position
  (`item_id`, `content_index`, `audio_end_ms`).
- Relay sends Foundry `conversation.item.truncate` after `response.cancel`.
- Relay sends `output_audio.started` to the device, allowing the headset test to track which output item
  has played and how far.
- On a tap, Stack-chan first runs `M5.Speaker.stop()` and does not play PCM that arrives due to a race.

### 7. It returned to listening by itself after pausing

Pausing raced with the logic that automatically resumes listening after a response completes, and stale
asynchronous work sent `audio.start`. The display could show listening while the internal conversation
state was stopped.

**Response**

- A conversation revision was introduced in the headset test.
- Automatic resume runs only when the revision recorded before waiting matches the revision after waiting.
- The listening state alone does not enable the microphone; `conversation_active` is also required.

**Operational meaning**

After pausing, the system remains idle. Resume it with the "start conversation" action.

### 8. An error from an already canceled response was shown as a failure

`Cancellation failed: no active response found` occurred immediately after stopping.

**Cause**

The response had completed normally before the cancellation request. This is a race between stopping and
response completion.

**Response**

When this error is received while cancellation is pending, treat it as an expected race and do not send
`REALTIME_ERROR` to the device.

### 9. Web search failed after 20 seconds

During Web search, `asyncio.wait_for(..., 20)` fired and produced `TimeoutError` before the search itself
completed.

**Causes**

- Responses API Web search can exceed 20 seconds because it performs retrieval and grounding.
- Synchronous HTTP work running through `asyncio.to_thread()` remains in a background thread even when
  `wait_for` is canceled.

**Response and current specification**

- Removed Relay's fixed 20-second timeout.
- Use the OpenAI client's HTTP timeout centrally.
- Added `WEB_SEARCH_TIMEOUT_SECONDS`, defaulting to 90 seconds with a configurable range of 5 to 300 seconds.
- A real `gpt-5.6-terra` search was confirmed to complete in about 10 seconds with 1 source.

### 10. After Foundry reconnected, listening produced no response

After the Foundry connection reset, the device returned to listening, but output for the next utterance
was discarded.

**Cause**

The "canceling/output suppressed" flags set for the previous Foundry connection remained on the new
WebSocket connection. A new response was mistaken for the old canceled response.

**Response**

- Initialize output suppression, pending cancellation, and output-item tracking for each Foundry connection.
- Stop sending a reconnection as a false `response.done`; add `session.reconnected` instead.
- The headset test and Stack-chan resume listening on `ready` after this event only when the conversation
  is active.

### 11. Aligned the Stack-chan implementation with the current Relay specification

**Changes**

- A tap while listening sends `conversation.pause` instead of `audio.stop`.
- Taps during thinking / searching / speaking use the same pause path.
- Handle `session.reconnected` separately from normal response completion.
- Added `RELAY_USE_TLS`. The sample settings are `RELAY_PORT=8080` and `RELAY_USE_TLS=0`.
- Replaced ArduinoJson 7's deprecated `StaticJsonDocument` with `JsonDocument`. This also avoids losing
  data when long JSON containing search sources exceeds the fixed 512-byte capacity.

### 12. Wi-Fi reconnection appeared to remain "connecting"

When Wi-Fi connection failed, the display repeatedly showed a connection attempt, making the failure
reason and retry status unclear to the user.

**Cause and response**

- The connection-attempt limit and post-failure display transition were not explicit.
- Disabled Wi-Fi sleep because it could affect connection stability.
- Limited retries and explicitly show `disconnected` when the limit is reached.
- The Status screen shows Wi-Fi and Relay connection state and the latest concise error.

**Design decision**

Automate recovery without hiding failure behind a permanent "connecting" display. Combine automatic
retry with an observable failure state.

### 13. Physical-device playback had noise, omissions, and fast-forward behavior

Even when communication succeeded, audio could become noisy, stop partway through playback, or sound as
if the queue had been consumed all at once.

**Causes**

- Audio frames could exceed the receive-size constraint of the ESP-side WebSocket client.
- The receive path could reuse or overwrite a buffer while the asynchronous speaker was still playing it.
- Network reception and speaker playback progress at different rates.

**Response and current specification**

- PCM from Relay to the device is split into binary frames of at most 8 KiB.
- CoreS3 uses multiple playback buffers and does not overwrite a region while it is playing.
- Received frames are treated as continuous PCM in arrival order, not as message-level units.

**Design decision**

Separate audio transfer units from playback units. A WebSocket frame boundary must not be interpreted as
a semantic audio boundary.

### 14. Updating expressions and diagnostics on the same screen caused flicker

Continuously drawing tabs and diagnostic information over a lip-synced face made both the expression and
information display appear to flicker.

**Specification changes**

- Split the screen into Status, Face, and Settings.
- Face hides tabs and diagnostics, prioritizing expression and lip sync.
- Horizontal swipes change screens, and Settings adjusts device-side volume.
- Relay's `emotion` event draws `neutral`, `happy`, `sad`, `angry`, `surprised`, and `sleepy`.
- During Web search, use a calm researching animation rather than excessively rapid eye movement.

**Design decision**

Separate the rendering surfaces for an avatar updated every frame and an information UI updated less
frequently. Even when a state can be represented technically, tune cuteness, reassurance, and readability
on the physical device.

### 15. Model names and settings could diverge between implementation and documentation

After the models changed, old model names could remain in defaults or procedures.

**Response**

- Standardized the voice conversation model on `gpt-realtime-2.1` and the Responses API search model on
  `gpt-5.6-terra`.
- Searched and updated application settings, `.env.example`, Bicep, parameter examples, tests, README,
  and operations documents together.

**Design decision**

A model change is not only an application-code change. Update and inspect deployment configuration and
user procedures as one configuration surface.

### 16. Reviewed exclusions and generated artifacts before publishing on GitHub

Before publication, credential templates and actual values, build artifacts, and local certificates
needed to be distinguished clearly.

**Response**

- Excluded `.env.*` and `secrets*.hpp`, explicitly allowing only sample files.
- Excluded private-key and certificate formats (`.pem`, `.key`, `.p12`, `.pfx`).
- Excluded Bicep-generated `infra/main.json`.
- Checked tracked and untracked publication candidates for known API-key formats, GitHub tokens, Azure
  connection strings, and private keys.
- Re-ran Firmware and Bicep builds and confirmed that CI validates Relay, Firmware, and Bicep separately.

**Operational note**

`.env.example`, `secrets.example.hpp`, and `infra/main.bicepparam` are public templates and must not contain
actual values. A public Relay uses WSS and device-token authentication. Rotate the device token if credentials
or a device may have been exposed.

### 17. The device WebSocket disconnected about 30 seconds after playback ended

After audio playback ended, the screen returned to idle, but the Relay connection dropped and reconnected
automatically several seconds later. Because the Relay and Foundry processes continued running, it appeared
that `response.done` was ending the session.

**Facts confirmed in logs**

- Relay keeps the device WebSocket after `response.done` and does not issue a disconnect command.
- On Relay, the `device_to_realtime` task completed first, with close code `1005` (no code in the close frame).
- CoreS3 recorded `Connection lost` and then reconnected automatically.
- A Wi-Fi disconnection transition and Foundry session termination were not observed at the same time.

**Cause assessment**

After playback, PCM transfer stops and the ESP32-to-Relay WebSocket path becomes idle. On this path, a
delayed response to a short WebSocket liveness check or reclamation of an idle TCP connection can occur.
The disconnect point was the idle transport connection, not application-level `response.done`.

**Response and current specification**

- CoreS3 sends the application control message `ping` to Relay every 10 seconds.
- It also enables WebSocket protocol Ping/Pong heartbeat at a 10-second interval. Heartbeat timeout itself
  is not a disconnect condition; existing reconnection handles an actual TCP disconnect.
- Relax local Relay (uvicorn) WebSocket ping to a 60-second interval and timeout to 30 seconds.
- Physical-device logs output `Relay keepalive sent`, the reason length on disconnect, and `response.done`.
- Relay records `response.done`, the completed connection task name, and the device close code.

**Verification**

1. Hold a conversation and wait at least 40 seconds after audio playback ends.
2. Confirm that `Relay keepalive sent` continues and `Relay WebSocket disconnected` does not appear.
3. If it recurs, check `connection task completed` and `device disconnected` in the Relay log.
   For `tasks=device_to_realtime` / `code=1005`, treat it as a device-side transport disconnect rather
   than completion of a Relay or Foundry response.

**Operational note**

Update both the firmware and `relay/local_manager.py`. Restarting Relay Manager alone does not apply the
Relay setting change. Stop and start Relay itself from the management UI, or start Relay after
`run-manager.ps1 -Restart`.

### 18. Integrated the K151 neck servos and ESP-NOW MiniJoyC remote

M5Stack K151 is not the PWM servo configuration often used by custom Stack-chan builds. It is a finished
StackChan using CoreS3 and serial servos with feedback. Treating it initially as an I2C-connected MiniJoyC
does not communicate with the official wireless controller.

**Hardware prerequisites**

- The neck servos are SCS0009 units connected to the serial servo bus using CoreS3 GPIO 6 (Servo_TX) /
  GPIO 7 (Servo_RX).
- Horizontal (X/yaw) is the continuous-rotation axis on servo ID 1, and vertical (Y/pitch) is the movable
  axis on servo ID 2.
- It does not use an I2C PWM servo driver such as PCA9685. GPIO 11/12 are also not for servo control.
- Limit the vertical axis to a safe range of 5 to 85 degrees (50 to 850 internally).

**Implementation**

- Delegate actual neck movement to `M5StackChan.Motion` from `StackChan-BSP`. Do not substitute facial
  expression changes for remote-control motion.
- The official ESP-NOW wireless controller sends an 8-byte control payload after a 20-byte Espressif ESPNOW
  header. The first byte is the target ID; the following little-endian values are yaw, pitch, and speed;
  the final byte is button/laser state.
- The receiver accepts only target ID `0` (broadcast) or the configured Receiver ID, clamping yaw to
  -1280 to 1280 and pitch to 50 to 850.
- Apply the latest received values to Motion only on the FACE screen, preventing unintended neck movement
  on the Settings screen or elsewhere.

**ESP-NOW and Wi-Fi notes**

ESP-NOW shares the same radio and channel as Wi-Fi. Initialize ESP-NOW after connecting to Relay Wi-Fi and
use that Wi-Fi channel. Forcing channel 1 breaks Relay communication when the Relay AP uses another channel.

- Show the actual Wi-Fi channel and Receiver ID on the Settings screen.
- Set the MiniJoyC transmitter channel to that displayed value and the destination ID to the Receiver ID or 0.
- The current default Receiver ID is 1. If the ID is changed on screen, update the transmitter to match.

**Verification procedure**

1. Connect K151 to Relay Wi-Fi and check the channel and Receiver ID on the Settings screen.
2. Configure MiniJoyC with the same channel and target ID.
3. Move the stick on the FACE screen and confirm that the neck physically moves.
4. Confirm that the vertical axis stays within the safe range and that the remote can control only the neck
   during a Relay audio session.

### 19. Enabled conversation and sleep controls from the CoreS3 top touch surface

Added an operation that returns to the conversation regardless of the current screen, and another that
explicitly stops conversation and playback before moving to a resting pose.

**Current specification**

- 1 tap on the top moves to the Face screen and starts listening when Relay is connected.
- 2 top taps within 2 seconds stop the microphone and playback, send `conversation.pause`, and move the
  neck into a lowered sleep pose.
- Settings volume buttons adjust 1 step on a short tap and continuously when held for at least 500 ms.
- Existing screen-tap conversation controls, the camera area, and horizontal swipes remain unchanged.

### 20. Added staged idle behavior to the cat face

To keep the device from appearing frozen between conversations, Face idle
behavior is divided into three stages: `Active`, `LookingAround`, and `Sleeping`.

**Current specification**

- Automatic idle behavior runs only on Face in `ready` or `listening` while the
  camera wipe is hidden.
- After 30 seconds without interaction or detected speech, enter `LookingAround`
  and move the cat gaze and neck randomly every 2 to 15 seconds.
- Five minutes after looking around begins, enter `Sleeping`, return the neck
  home, and draw closed eyes, breathing motion, and a `Z` display.
- Return to `Active` after touch, or after three consecutive frames of detected
  voice activity while listening.
- Automatic sleep does not stop the conversation. Only explicit sleep from a
  top double tap sends `conversation.pause` and stops capture and playback.

**Design decision**

Treat avatar idle expression and conversation-session suspension as separate
states. A visually sleeping avatar can wake naturally on speech while it is
listening; the conversation stops only when the user explicitly suspends it.

## Expected behavior after flashing the firmware

1. After startup, connect to Relay and send `hello`. The screen enters idle state.
2. Starting a conversation with a tap enables the microphone and enters listening.
3. After an utterance, transition through thinking / searching / speaking and stop the microphone during them.
4. When the response ends naturally, automatically return to the next listening state after speaker playback ends.
5. A tap in any conversation state stops playback and the microphone, sends `conversation.pause` to Relay,
   and enters idle state. Do not automatically resume before the next tap.
6. If only Foundry reconnects, resume listening after `session.reconnected` → `ready` when the conversation
   is active.

## Known constraints

- A Foundry WebSocket reconnection creates a new Realtime session. Conversation history itself is not
  restored to the new connection.
- Without AEC, full-duplex voice interruption is not enabled by default.
- Because normal output is not dropped, latency increases when generation is substantially faster than
  playback. Monitor backlog warnings.
- Compilation for CoreS3 has been verified, but extended physical-device microphone and speaker operation
  still requires separate verification.

## Verification and operations procedure

1. After Relay changes, restart with `relay\\run-manager.ps1 -Restart`.
2. Set the LAN IP, port, and `RELAY_USE_TLS` in `stackchan/include/secrets.hpp`.
3. In the `stackchan` directory, run `pio run`, then run `pio run -t upload` after connecting the device.
4. When problems occur, copy the Relay and headset logs from the management UI and inspect state transitions
   (`ready` / `listening` / `thinking` / `searching` / `speaking`) and the immediately preceding event.
