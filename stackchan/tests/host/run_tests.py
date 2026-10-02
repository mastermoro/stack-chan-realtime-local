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
    )
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
            ],
            ["relay_client.cpp", "usb_transport.cpp"],
        )
    )

failures = 0
with tempfile.TemporaryDirectory(prefix="stackchan-host-") as directory:
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
