import asyncio

from app.gateway.connections import DeviceConnectionRegistry


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed: tuple[int, str] | None = None

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)


def test_new_connection_supersedes_previous_transport() -> None:
    async def scenario() -> None:
        registry = DeviceConnectionRegistry()
        first = FakeWebSocket()
        second = FakeWebSocket()

        await registry.claim("stackchan-001", first)  # type: ignore[arg-type]
        await registry.claim("stackchan-001", second)  # type: ignore[arg-type]

        assert first.closed == (4000, "superseded by a new transport")
        assert second.closed is None

        # A stale session must not release the currently active transport.
        await registry.release("stackchan-001", first)  # type: ignore[arg-type]
        third = FakeWebSocket()
        await registry.claim("stackchan-001", third)  # type: ignore[arg-type]
        assert second.closed == (4000, "superseded by a new transport")

    asyncio.run(scenario())
