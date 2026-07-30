from pathlib import Path

import pytest

from local_manager import HeadsetSupervisor, LocalManagerError, RelaySupervisor


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
