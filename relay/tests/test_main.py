from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_and_capability_endpoints_are_available() -> None:
    assert client.get("/healthz").json() == {"status": "ok"}

    capabilities = client.get("/capabilities").json()

    assert capabilities["foundry"] is False
    assert capabilities["local_browser_tool"] is False
    assert capabilities["browser_allowed_domains"] == []
