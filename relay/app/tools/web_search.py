import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai import OpenAI

from app.config.settings import Settings
from app.protocol.messages import Source

AZURE_OPENAI_SCOPE = "https://ai.azure.com/.default"


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
            tool_choice="auto",
            include=["web_search_call.action.sources"],
            input=(
                "次の質問についてWeb検索を行い、音声会話に戻しやすい簡潔な日本語で回答してください。"
                "事実は検索結果に基づき、推測で補完しないでください。\n\n質問: " + query
            ),
        )
        return self.parse_response(response)

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
        data = response.model_dump() if hasattr(response, "model_dump") else response
        answer = getattr(response, "output_text", None) or data.get("output_text", "")
        sources: list[Source] = []
        seen: set[str] = set()

        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") != "output_text":
                    continue
                if not answer:
                    answer = content.get("text", "")
                for annotation in content.get("annotations", []):
                    if annotation.get("type") != "url_citation":
                        continue
                    url = annotation.get("url", "")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    sources.append(Source(title=annotation.get("title", ""), url=url))

        return WebSearchResult(answer=answer.strip(), sources=sources)
