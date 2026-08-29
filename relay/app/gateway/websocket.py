import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config.settings import get_settings
from app.gateway.auth import DeviceAuthenticationError, DeviceAuthenticator
from app.gateway.connections import DeviceConnectionRegistry
from app.session.orchestrator import RelaySession

router = APIRouter()
logger = logging.getLogger(__name__)
connections = DeviceConnectionRegistry()


@router.websocket("/v1/realtime")
async def realtime_gateway(websocket: WebSocket) -> None:
    settings = get_settings()
    authenticator = DeviceAuthenticator(settings)
    try:
        device_id = authenticator.authenticate(websocket)
    except DeviceAuthenticationError:
        await websocket.close(code=1008, reason="unauthorized")
        return

    await websocket.accept()
    await connections.claim(device_id, websocket)
    session = RelaySession(websocket, device_id, settings)
    try:
        await session.run()
    except WebSocketDisconnect as exc:
        logger.info("device disconnected: %s code=%s", device_id, exc.code)
    except Exception:
        logger.exception("relay session failed: %s", device_id)
        try:
            await websocket.send_json(
                {"type": "error", "code": "SESSION_FAILED", "message": "session terminated"}
            )
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        await connections.release(device_id, websocket)
