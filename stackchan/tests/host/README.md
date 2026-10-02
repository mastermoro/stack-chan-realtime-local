# Native firmware regression tests

These tests compile the production `relay_client.cpp` and `usb_transport.cpp`
with a C++17 host compiler. Small Arduino/Serial/WebSocket stubs replace hardware
I/O; JSON parsing and serialization use the real pinned ArduinoJson library.
No ESP32, network connection, credentials, or test framework is required.

From `stackchan/`, after `pio run` has installed the project dependencies:

```sh
python tests/host/run_tests.py
```

Alternatively, point to the `src` directory of an existing ArduinoJson 7.4.3
checkout without installing an ESP32 toolchain:

```sh
python tests/host/run_tests.py --arduinojson /path/to/ArduinoJson/src
```

The USB-only suite does not need ArduinoJson:

```sh
python tests/host/run_tests.py --usb-only
```

Coverage includes both transport callbacks, queued stale state/PCM/done/emotion,
exact acknowledgement matching and malformed IDs, repeated interruptions,
failed sends, reconnects and duplicate session notifications, stale notices,
and outbound microphone audio while the response fence is closed. The USB tests
exercise actual COBS/CRC framing with continuous producers, byte/time/packet
receive budgets, partial maximum-size frames, and clock wraparound.

Lost-ack tests verify that the five-second fence deadline disconnects the old
transport instead of unmuting it, including clock rollover, repeated interrupts,
late/stale acknowledgements, transport switching, and correlated USB recovery.
Queued USB close/open frames cannot clear an outstanding fence. Recovery status
discloses the reset conversation context through the new session's handshake.

The runner also extracts the production Face control functions from `main.cpp`
and compiles them with small audio/relay I/O fakes. These regressions check that
successful top/screen interrupts immediately re-enable capture, failed sends
remain in Error, and upstream reconnect preserves capture plus its reset notice.

The budgets bound receive work between opportunities to service touch input;
a frame handler already in progress still runs to completion. These are
scheduling and protocol regressions, not a measurement of physical touch or
speaker latency. Validate responsiveness on hardware as well.
