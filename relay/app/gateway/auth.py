import hmac

from fastapi import WebSocket

from app.config.settings import Settings


class DeviceAuthenticationError(Exception):
    pass


class DeviceAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self._tokens = settings.device_tokens_json

    def authenticate(self, websocket: WebSocket) -> str:
        device_id = websocket.headers.get("x-device-id", "").strip()
        auth = websocket.headers.get("authorization", "").strip()
        if not device_id or not auth.lower().startswith("bearer "):
            raise DeviceAuthenticationError("missing device credentials")

        presented = auth[7:].strip()
        expected = self._tokens.get(device_id)
        if not expected or not hmac.compare_digest(presented, expected):
            raise DeviceAuthenticationError("invalid device credentials")
        return device_id
