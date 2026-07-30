import asyncio

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
