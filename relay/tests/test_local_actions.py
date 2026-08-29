import asyncio
import json

import pytest

from app.config.settings import Settings
from app.tools.local_actions import LocalActionError, LocalActionExecutor


def test_browser_tool_is_not_registered_by_default() -> None:
    executor = LocalActionExecutor(Settings())

    assert executor.tool_definitions() == []
    assert not executor.supports("open_browser_url")


def test_browser_tool_opens_valid_https_url() -> None:
    opened: list[str] = []
    executor = LocalActionExecutor(
        Settings(
            local_browser_tool_enabled=True,
            local_browser_allowed_domains="example.com",
        ),
        browser_launcher=lambda url: opened.append(url) is None,
    )

    result = asyncio.run(
        executor.execute("open_browser_url", {"url": "https://docs.example.com/guide"})
    )

    assert result == {
        "status": "opened",
        "url": "https://docs.example.com/guide",
    }
    assert opened == ["https://docs.example.com/guide"]


def test_browser_tool_rejects_commands_and_unlisted_hosts() -> None:
    executor = LocalActionExecutor(
        Settings(
            local_browser_tool_enabled=True,
            local_browser_allowed_domains="example.com",
        ),
        browser_launcher=lambda _: True,
    )

    with pytest.raises(LocalActionError, match="only http and https"):
        asyncio.run(executor.execute("open_browser_url", {"url": "file:///C:/Windows"}))

    with pytest.raises(LocalActionError, match="not allowed"):
        asyncio.run(
            executor.execute("open_browser_url", {"url": "https://untrusted.invalid/"})
        )


def test_browser_tool_rejects_url_credentials() -> None:
    executor = LocalActionExecutor(
        Settings(local_browser_tool_enabled=True),
        browser_launcher=lambda _: True,
    )

    with pytest.raises(LocalActionError, match="credentials"):
        asyncio.run(
            executor.execute(
                "open_browser_url",
                {"url": "https://user:password@example.com/"},
            )
        )


def test_windows_browser_url_is_handed_to_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.tools.local_actions.os.name", "nt")
    requests: list[tuple[str, str, str, dict[str, str]]] = []

    class FakeResponse:
        status = 200

        @staticmethod
        def read() -> bytes:
            return b""

    class FakeConnection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            assert (host, port, timeout) == ("127.0.0.1", 8787, 6)

        def request(
            self, method: str, path: str, body: str, headers: dict[str, str]
        ) -> None:
            requests.append((method, path, body, headers))

        @staticmethod
        def getresponse() -> FakeResponse:
            return FakeResponse()

        @staticmethod
        def close() -> None:
            pass

    monkeypatch.setattr("app.tools.local_actions.HTTPConnection", FakeConnection)

    assert LocalActionExecutor._open_browser("https://example.com/")
    assert requests == [
        (
            "POST",
            "/api/browser/open",
            json.dumps({"url": "https://example.com/"}),
            {"Content-Type": "application/json"},
        )
    ]


def test_windows_browser_broker_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.tools.local_actions.os.name", "nt")

    class FakeResponse:
        status = 400

        @staticmethod
        def read() -> bytes:
            return b""

    class FakeConnection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        @staticmethod
        def request(*_args: object, **_kwargs: object) -> None:
            pass

        @staticmethod
        def getresponse() -> FakeResponse:
            return FakeResponse()

        @staticmethod
        def close() -> None:
            pass

    monkeypatch.setattr("app.tools.local_actions.HTTPConnection", FakeConnection)

    assert not LocalActionExecutor._open_browser("https://example.com/")
