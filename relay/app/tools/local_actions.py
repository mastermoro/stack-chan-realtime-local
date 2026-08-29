import asyncio
import json
import logging
import os
import webbrowser
from collections.abc import Callable
from http.client import HTTPConnection
from typing import Any
from urllib.parse import urlsplit

from app.config.settings import Settings

logger = logging.getLogger(__name__)


class LocalActionError(ValueError):
    pass


class LocalActionExecutor:
    """Executes an explicit allow-list of actions on the Windows Relay host."""

    def __init__(
        self,
        settings: Settings,
        *,
        browser_launcher: Callable[[str], bool] | None = None,
    ) -> None:
        self._settings = settings
        self._browser_launcher = browser_launcher or self._open_browser

    def tool_definitions(self) -> list[dict[str, Any]]:
        if not self._settings.local_browser_tool_enabled:
            return []
        return [
            {
                "type": "function",
                "name": "open_browser_url",
                "description": (
                    "Open an HTTP or HTTPS URL in the default browser on the local Windows "
                    "Relay PC. Use only when the user explicitly asks to open or show a page."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "Complete http:// or https:// URL to open",
                        }
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            }
        ]

    def supports(self, name: str) -> bool:
        return name == "open_browser_url" and self._settings.local_browser_tool_enabled

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.supports(name):
            raise LocalActionError(f"local action is disabled or unsupported: {name}")

        url = self._validate_browser_url(arguments.get("url"))
        opened = await asyncio.to_thread(self._browser_launcher, url)
        if not opened:
            raise LocalActionError("the default browser did not accept the URL")

        logger.info("local browser opened host=%s", urlsplit(url).hostname)
        return {"status": "opened", "url": url}

    def _validate_browser_url(self, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise LocalActionError("url must be a non-empty string")
        url = value.strip()
        if len(url) > 2048:
            raise LocalActionError("url is too long")

        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise LocalActionError("only http and https URLs are allowed")
        if not parsed.hostname:
            raise LocalActionError("url must include a host")
        if parsed.username or parsed.password:
            raise LocalActionError("credentials in URLs are not allowed")

        allowed = self._settings.browser_allowed_domains
        hostname = parsed.hostname.lower().rstrip(".")
        if allowed and not any(
            hostname == domain or hostname.endswith("." + domain) for domain in allowed
        ):
            raise LocalActionError(f"host is not allowed: {hostname}")
        return url

    @staticmethod
    def _open_browser(url: str) -> bool:
        if os.name == "nt":
            connection = HTTPConnection("127.0.0.1", 8787, timeout=6)
            try:
                connection.request(
                    "POST",
                    "/api/browser/open",
                    body=json.dumps({"url": url}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response.read()
                if response.status == 200:
                    return True
                logger.warning("browser broker rejected request status=%s", response.status)
                return False
            except OSError as exc:
                logger.warning("browser broker request failed: %s", exc)
                return False
            finally:
                connection.close()
        return webbrowser.open(url, new=2, autoraise=True)
