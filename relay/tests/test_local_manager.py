import asyncio
from pathlib import Path

import pytest

from local_manager import (
    BrowserBroker,
    HeadsetSupervisor,
    LocalManagerError,
    ManagerShutdownController,
    RelaySupervisor,
    UsbBridgeSupervisor,
    shutdown,
)


def test_stopped_supervisor_reports_lan_relay_url(tmp_path: Path) -> None:
    supervisor = RelaySupervisor(tmp_path, relay_port=8090, log_directory=tmp_path / "logs")

    assert supervisor.status() == {
        "running": False,
        "managed": False,
        "reachable": False,
        "pid": None,
        "started_at": None,
        "last_exit_code": None,
        "relay_url": "http://0.0.0.0:8090",
    }


def test_start_requires_relay_virtual_environment(tmp_path: Path) -> None:
    supervisor = RelaySupervisor(tmp_path, log_directory=tmp_path / "logs")

    with pytest.raises(LocalManagerError, match="virtual environment"):
        supervisor.start()


def test_log_tail_returns_requested_number_of_lines(tmp_path: Path) -> None:
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    log_path = log_directory / "relay.log"
    log_path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    supervisor = RelaySupervisor(tmp_path, log_directory=log_directory)
    supervisor._log_path = log_path

    assert supervisor.read_logs(2) == {"text": "two\nthree"}


def test_headset_test_requires_relay_virtual_environment(tmp_path: Path) -> None:
    supervisor = HeadsetSupervisor(tmp_path, log_directory=tmp_path / "logs")

    with pytest.raises(LocalManagerError, match="virtual environment"):
        supervisor.start(input_device=None, output_device=None)


def test_usb_bridge_requires_relay_virtual_environment(tmp_path: Path) -> None:
    supervisor = UsbBridgeSupervisor(tmp_path, log_directory=tmp_path / "logs")

    with pytest.raises(LocalManagerError, match="virtual environment"):
        supervisor.start()


def test_stopped_usb_bridge_has_no_child_status(tmp_path: Path) -> None:
    supervisor = UsbBridgeSupervisor(tmp_path, log_directory=tmp_path / "logs")

    assert supervisor.status() == {
        "running": False,
        "pid": None,
        "started_at": None,
        "last_exit_code": None,
        "bridge": None,
    }


def test_browser_broker_uses_interactive_explorer(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[list[str]] = []

    class FakeProcess:
        def wait(self, timeout: float) -> int:
            assert timeout == 5
            return 1

    def fake_popen(arguments: list[str]) -> FakeProcess:
        launched.append(arguments)
        return FakeProcess()

    monkeypatch.setattr("local_manager.subprocess.Popen", fake_popen)

    assert BrowserBroker.open("https://example.com/")
    assert launched == [["explorer.exe", "https://example.com/"]]


def test_manager_shutdown_request_runs_callback_after_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[float] = []

    class FakeTimer:
        def __init__(self, delay: float, callback: object) -> None:
            requested.append(delay)
            self._callback = callback

        def start(self) -> None:
            self._callback()

    monkeypatch.setattr("local_manager.threading.Timer", FakeTimer)
    callbacks: list[str] = []

    ManagerShutdownController(lambda: callbacks.append("shutdown")).request()

    assert requested == [0.25]
    assert callbacks == ["shutdown"]


def test_shutdown_stops_managed_processes_before_requesting_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeSupervisor:
        def __init__(self, name: str) -> None:
            self._name = name

        def stop(self) -> None:
            calls.append(self._name)

    class FakeShutdownController:
        def request(self) -> None:
            calls.append("manager")

    monkeypatch.setattr("local_manager.headset_supervisor", FakeSupervisor("headset"))
    monkeypatch.setattr("local_manager.usb_bridge_supervisor", FakeSupervisor("usb"))
    monkeypatch.setattr("local_manager.supervisor", FakeSupervisor("relay"))
    monkeypatch.setattr("local_manager.shutdown_controller", FakeShutdownController())

    assert asyncio.run(shutdown()) == {"status": "shutting_down"}
    assert calls == ["headset", "usb", "relay", "manager"]
