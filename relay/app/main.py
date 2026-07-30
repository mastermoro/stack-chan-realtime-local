import logging

from fastapi import FastAPI

from app.config.settings import get_settings
from app.gateway.websocket import router as websocket_router

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title="Stack-chan Realtime Local Relay", version="0.2.0")
app.include_router(websocket_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {"status": "ready" if settings.foundry_configured else "not_configured"}


@app.get("/capabilities")
async def capabilities() -> dict[str, object]:
    return {
        "foundry": settings.foundry_configured,
        "local_browser_tool": settings.local_browser_tool_enabled,
        "browser_allowed_domains": settings.browser_allowed_domains,
    }
