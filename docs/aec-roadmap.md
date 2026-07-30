# AEC and full-duplex implementation roadmap

This repository is local-first at the Relay layer, but acoustic echo
cancellation still runs on the CoreS3. The Windows Relay remains responsible
for Foundry sessions, Function Calling, and local PC actions.

## Invariants

- The existing half-duplex path remains the safe fallback.
- Network audio remains PCM16, 24 kHz, mono.
- AEC processing uses a separate 16 kHz internal boundary.
- The playback reference is copied only from PCM accepted by the speaker TX
  path, after volume and format conversion.
- Audio tasks do not perform JSON parsing, browser actions, screen drawing,
  allocation, file writes, or network sends.
- A failure reduces capability instead of rebooting the device.

## Target pipeline

```text
Foundry -> Windows Relay -> 24 kHz playback ring
  -> volume/limiter -> speaker TX
  -> accepted-TX reference timeline -> 24-to-16 kHz

CoreS3 mic RX -> capture ring -> 16 kHz
  -> PassThroughProcessor / EspSrAecProcessor
  -> 16-to-24 kHz -> Windows Relay -> Foundry
```

## Planned firmware files

```text
stackchan/include/audio/audio_types.hpp
stackchan/include/audio/audio_device.hpp
stackchan/include/audio/spsc_audio_ring.hpp
stackchan/include/audio/audio_pipeline.hpp
stackchan/include/audio/audio_processor.hpp
stackchan/include/audio/playback_clock.hpp
stackchan/include/audio/reference_timeline.hpp
stackchan/include/audio/audio_diagnostics.hpp
stackchan/include/audio/fallback_policy.hpp
stackchan/include/conversation_controller.hpp

stackchan/src/audio/core_s3_full_duplex_device.cpp
stackchan/src/audio/pass_through_processor.cpp
stackchan/src/audio/streaming_resampler.cpp
stackchan/src/audio/audio_pipeline.cpp
stackchan/src/conversation_controller.cpp
```

Pure data structures and state machines must have PlatformIO native tests
before they are connected to CoreS3 hardware.

## Milestones

### A0 — Reproducible baseline

- Pin the Espressif platform and firmware libraries.
- Build the unchanged half-duplex firmware in CI.
- Record RAM and Flash use.

Status: implemented in this repository. The pinned baseline builds for CoreS3.

### A1 — Diagnostics

Add one-second aggregate metrics:

- mic, reference, and processor-output RMS/peak
- capture/playback/reference ring fill and high-water marks
- I2S timeout/drop counts
- received, queued, submitted, and estimated-played samples
- AEC/processor average and maximum execution time
- internal heap, largest block, PSRAM, and task stack low-water marks

Detailed PCM capture remains opt-in and must never be committed.

### A2 — Fixed audio rings and clocks

- Add fixed-capacity SPSC rings with no runtime allocation.
- Track 64-bit sample positions rather than relying on `millis()`.
- Separate WebSocket frame boundaries from processing frame boundaries.
- Add native tests for wrap, overflow, underflow, and time conversion.

### A3 — Audio I/O ownership

- Move all microphone and speaker lifecycle management out of `main.cpp`.
- Preserve `M5AudioHal` as the legacy half-duplex backend.
- Add an `AudioDevice` contract for capture, TX acceptance, queue depth,
  capabilities, counters, and stop/restart.
- Route current behavior through `AudioPipeline + PassThroughProcessor`.

### A4 — CoreS3 full-duplex hardware gate

- Add a feature-gated backend that owns shared I2S RX/TX and both codecs.
- Validate simultaneous ES7210 capture and AW88298 playback without AEC.
- Run 10-second functional and 30-minute soak tests with Wi-Fi, camera, and
  servo load.

Do not proceed when RX or TX stops, repeated drops occur, or the device resets.
The default remains half duplex until this hardware gate passes.

### A5 — Playback reference and synchronization

- Tap only speaker-TX-accepted PCM.
- Track received, queued, submitted, DMA-buffered, and estimated-played samples.
- Store reference samples on a timestamped timeline.
- Add a diagnostic delay search over 0–150 ms.
- Observe long-running mic/playback clock drift and reference fill.

### A6 — Conversation and fallback states

Keep two independent state machines:

```text
Conversation:
Paused -> Listening -> Thinking/Searching -> Speaking -> Interrupting

Audio pipeline:
Stopped -> Starting -> HalfDuplex/FullDuplex
                    -> Degraded -> Restarting -> Failed
```

Use `output_audio.started` item IDs and estimated played samples when sending
`conversation.pause`, so the Relay can cancel and truncate unheard output.

Fallback order:

1. reduce camera/servo/diagnostic load
2. reduce AEC complexity
3. disable local barge-in
4. restart the audio pipeline once
5. return to legacy half duplex

Require a stable interval before restoring a higher mode to avoid flapping.

### A7 — AEC processing boundary

- Add stateful 24/16 kHz resampling.
- Adapt arbitrary I2S frames to the processor-required chunk size.
- Verify exact sample counts over 10 seconds and long-running streams.
- Keep `PassThroughProcessor` as the comparison and fallback implementation.

### A8 — ESP-SR AEC

Initial candidate:

- one mic and one playback reference
- PCM16 at 16 kHz
- `AEC_MODE_FD_LOW_COST`
- filter length 4
- 16-byte aligned working buffers

Evaluate normal and aggressive nonlinear processing with raw mic, reference,
and processed output measurements. Two-mic and high-performance modes are
follow-up tuning, not baseline requirements.

## Acceptance tests

| Test | Requirement |
|---|---|
| Assistant audio only | no self-interruption |
| User interrupts at 30 cm | local playback stops within 200 ms |
| User interrupts at 1 m | normal speech is detected |
| Maximum speaker volume | no repeated false barge-in |
| Double talk | user speech remains intelligible |
| Camera, Wi-Fi, and servo active | no WDT or repeated I2S timeout |
| 30-minute soak | no monotonic reference drift |
| AEC init/runtime failure | conversation continues in half duplex |

## Recommended change sequence

1. diagnostics and native test environment
2. rings, sample clocks, and playback tracking
3. legacy half-duplex migration behind `AudioPipeline`
4. playback item tracking and accurate pause/truncate
5. feature-gated CoreS3 full-duplex backend
6. reference timeline, resampling, and fallback policy
7. ESP-SR AEC behind a disabled-by-default feature flag

