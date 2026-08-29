import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from openai import OpenAI

from app.config.settings import Settings
from app.protocol.messages import Source

AZURE_OPENAI_SCOPE = "https://ai.azure.com/.default"
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WebSearchResult:
    answer: str
    sources: list[Source]

    def as_tool_output(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "sources": [source.model_dump() for source in self.sources],
        }


class WebSearchExecutor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._credential: Any | None = None
        self._client = self._build_client()

    def _build_client(self) -> "OpenAI":
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import OpenAI

        if self._settings.azure_openai_api_key:
            api_key: Any = self._settings.azure_openai_api_key.get_secret_value()
        else:
            self._credential = DefaultAzureCredential()
            api_key = get_bearer_token_provider(self._credential, AZURE_OPENAI_SCOPE)

        return OpenAI(
            base_url=self._settings.responses_base_url,
            api_key=api_key,
            timeout=self._settings.web_search_timeout_seconds,
        )

    async def search(self, query: str) -> WebSearchResult:
        if not query.strip():
            raise ValueError("query must not be empty")
        return await asyncio.to_thread(self._search_sync, query.strip())

    def _search_sync(self, query: str) -> WebSearchResult:
        tool: dict[str, Any] = {
            "type": "web_search",
            "user_location": self._user_location(),
        }
        filters: dict[str, list[str]] = {}
        if self._settings.allowed_domains:
            filters["allowed_domains"] = self._settings.allowed_domains
        if self._settings.blocked_domains:
            filters["blocked_domains"] = self._settings.blocked_domains
        if filters:
            tool["filters"] = filters

        response = self._client.responses.create(
            model=self._settings.azure_openai_responses_deployment,
            tools=[tool],
            # search_web is already an explicit Realtime function call. Do not
            # let the Responses model answer from memory without retrieving.
            tool_choice="required",
            include=["web_search_call.action.sources"],
            input=(
                "次の質問についてWeb検索を行い、音声会話に戻しやすい簡潔な日本語で回答してください。"
                "事実は検索結果に基づき、推測で補完しないでください。\n\n質問: " + query
            ),
        )
        result = self.parse_response(response)
        logger.info(
            "web search completed answer_chars=%s sources=%s",
            len(result.answer),
            len(result.sources),
        )
        return result

    def _user_location(self) -> dict[str, str]:
        location = {
            "type": "approximate",
            "country": self._settings.web_search_country,
            "timezone": self._settings.web_search_timezone,
        }
        if self._settings.web_search_city:
            location["city"] = self._settings.web_search_city
        if self._settings.web_search_region:
            location["region"] = self._settings.web_search_region
        return location

    @staticmethod
    def parse_response(response: Any) -> WebSearchResult:
        def field(value: Any, name: str, default: Any = None) -> Any:
            if isinstance(value, dict):
                return value.get(name, default)
            return getattr(value, name, default)

        answer = field(response, "output_text", "") or ""
        output = field(response, "output")
        if output is None and hasattr(response, "model_dump"):
            # Compatibility for older SDK response objects. Current Azure
            # objects expose typed output directly; model_dump on those emits
            # misleading warnings for ActionSearch.
            data = response.model_dump()
            output = data.get("output", [])
            answer = answer or data.get("output_text", "")
        output = output or []
        sources: list[Source] = []
        seen: set[str] = set()

        def add_source(value: Any) -> None:
            url = str(field(value, "url", "") or "")
            if not url or url in seen:
                return
            seen.add(url)
            title = str(field(value, "title", "") or "")
            if not title:
                title = urlsplit(url).hostname or url
            sources.append(Source(title=title, url=url))

        for item in output:
            item_type = field(item, "type", "")
            if item_type == "web_search_call":
                action = field(item, "action")
                for source in field(action, "sources", []) or []:
                    add_source(source)
                continue
            if item_type == "message":
                for content in field(item, "content", []) or []:
                    if field(content, "type", "") != "output_text":
                        continue
                    if not answer:
                        answer = field(content, "text", "") or ""
                    for annotation in field(content, "annotations", []) or []:
                        if field(annotation, "type", "") == "url_citation":
                            add_source(annotation)

        return WebSearchResult(answer=answer.strip(), sources=sources)
