import pytest

from app.config.settings import Settings
from app.gateway.auth import DeviceAuthenticationError, DeviceAuthenticator


class FakeWebSocket:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def test_auth_success() -> None:
    auth = DeviceAuthenticator(
        Settings(device_tokens_json={"stackchan-001": "secret"})
    )
    assert auth.authenticate(
        FakeWebSocket(
            {"x-device-id": "stackchan-001", "authorization": "Bearer secret"}
        )
    ) == "stackchan-001"


def test_auth_failure() -> None:
    auth = DeviceAuthenticator(Settings(device_tokens_json={"stackchan-001": "secret"}))
    with pytest.raises(DeviceAuthenticationError):
        auth.authenticate(
            FakeWebSocket(
                {"x-device-id": "stackchan-001", "authorization": "Bearer bad"}
            )
        )
