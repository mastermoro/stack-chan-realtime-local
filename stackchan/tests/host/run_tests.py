"""Run actual firmware transport/client code on the host, without ESP32 hardware.

After `pio run` (or `pio pkg install -e cores3`), ArduinoJson is available under
.pio/libdeps. A different installed ArduinoJson/src can be supplied explicitly.
"""

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

STACKCHAN = Path(__file__).resolve().parents[2]
HOST = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--arduinojson",
    type=Path,
    default=STACKCHAN / ".pio/libdeps/cores3/ArduinoJson/src",
)
parser.add_argument(
    "--usb-only", action="store_true", help="Run USB regressions without ArduinoJson"
)
args = parser.parse_args()


def main_control_functions() -> str:
    """Extract actual hardware-independent UI control flow, not a copied model."""
    source = (STACKCHAN / "src/main.cpp").read_text()

    def function(signature: str) -> str:
        start = source.index(signature + " {")
        brace = source.index("{", start)
        depth = 1
        end = brace + 1
        while depth:
            depth += (source[end] == "{") - (source[end] == "}")
            end += 1
        return source[start:end]

    touch = function("void handle_touch()")
    # The final state-dispatch block follows hardware-specific gesture, volume
    # and camera handling. Compile that exact block with host I/O fakes.
    touch = (
        "void handle_conversation_touch() {"
        + touch[touch.index("\n  if (state == AgentState::Ready)") :]
    )
    return "\n\n".join(
        [
            function("void start_listening()"),
            function("void handle_state(AgentState next)"),
            function("void enter_face_listening()"),
            function("void handle_session_reconnected()"),
            touch,
        ]
    )


suites = [
    (
        "usb_transport",
        [
            "sustained_stream",
            "time_budget",
            "packet_budget",
            "maximum_frame",
            "micros_wrap",
        ],
        ["usb_transport.cpp"],
    ),
    (
        "face_controls",
        [
            "top_success",
            "top_failed",
            "screen_success",
            "screen_failed",
            "context_reset",
        ],
        [],
    ),
]
if not args.usb_only:
    if not (args.arduinojson / "ArduinoJson.h").is_file():
        parser.error(
            "ArduinoJson headers unavailable; run pio run first or pass --arduinojson /path/to/ArduinoJson/src"
        )
    suites.append(
        (
            "relay_client",
            [
                "wifi_fence",
                "usb_fence",
                "exact_ack",
                "usb_exact_ack",
                "repeated_interrupts",
                "usb_repeated_interrupts",
                "failed_send",
                "usb_failed_send",
                "reconnect",
                "notices",
                "usb_notices",
                "audio_while_fenced",
                "lost_ack",
                "usb_lost_ack",
                "timeout_wrap",
                "usb_timeout_wrap",
                "timeout_repeated",
                "usb_timeout_repeated",
                "acked_deadline",
                "usb_acked_deadline",
                "timeout_switch",
                "usb_timeout_switch",
                "context_reset",
                "usb_context_reset",
            ],
            ["relay_client.cpp", "usb_transport.cpp"],
        )
    )

failures = 0
with tempfile.TemporaryDirectory(prefix="stackchan-host-") as directory:
    (Path(directory) / "main_face_controls_under_test.hpp").write_text(
        main_control_functions()
    )
    for suite, cases, sources in suites:
        output = Path(directory) / suite
        command = [
            os.environ.get("CXX", "c++"),
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-g",
            "-I",
            str(HOST / "stubs"),
            "-I",
            str(STACKCHAN / "include"),
            "-I",
            str(args.arduinojson),
            "-I",
            directory,
            str(HOST / f"test_{suite}.cpp"),
            *[str(STACKCHAN / "src" / source) for source in sources],
            "-o",
            str(output),
        ]
        subprocess.run(command, check=True)
        for case in cases:
            result = subprocess.run([str(output), case], timeout=15, check=False)
            failures += result.returncode != 0
raise SystemExit(1 if failures else 0)
