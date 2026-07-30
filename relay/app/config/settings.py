from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    azure_openai_endpoint: str = "https://example.openai.azure.com"
    azure_openai_realtime_deployment: str = "gpt-realtime-2.1"
    azure_openai_responses_deployment: str = "gpt-5.6-terra"
    azure_openai_api_key: SecretStr | None = None

    # Semantic VAD avoids ending a Japanese utterance on a short natural pause.
    # server_vad remains available for deployments that do not support semantic VAD.
    realtime_turn_detection_type: Literal["semantic_vad", "server_vad"] = "semantic_vad"
    realtime_vad_eagerness: Literal["low", "medium", "high", "auto"] = "low"
    realtime_vad_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    realtime_vad_prefix_padding_ms: int = Field(default=300, ge=0)
    realtime_vad_silence_duration_ms: int = Field(default=1000, ge=100)

    device_tokens_json: dict[str, str] = Field(default_factory=dict)

    web_search_country: str = "JP"
    web_search_timezone: str = "Asia/Tokyo"
    web_search_city: str = ""
    web_search_region: str = ""
    web_search_allowed_domains: str = ""
    web_search_blocked_domains: str = ""
    # Responses web_search can legitimately take longer than a conversational
    # turn because it has to retrieve and ground web results.
    web_search_timeout_seconds: int = Field(default=90, ge=5, le=300)

    # Windows-hosted Function Calling. Tools are registered individually so
    # enabling local actions never grants arbitrary command execution.
    local_browser_tool_enabled: bool = False
    local_browser_allowed_domains: str = ""

    log_level: str = "INFO"
    transcript_logging: bool = False

    @property
    def realtime_websocket_base_url(self) -> str:
        endpoint = self.azure_openai_endpoint.rstrip("/")
        if endpoint.startswith("https://"):
            endpoint = "wss://" + endpoint.removeprefix("https://")
        return endpoint + "/openai/v1"

    @property
    def responses_base_url(self) -> str:
        return self.azure_openai_endpoint.rstrip("/") + "/openai/v1/"

    @property
    def foundry_configured(self) -> bool:
        endpoint = self.azure_openai_endpoint.strip().lower()
        return bool(
            endpoint
            and "your-resource" not in endpoint
            and "example.openai.azure.com" not in endpoint
            and self.azure_openai_realtime_deployment.strip()
            and self.azure_openai_responses_deployment.strip()
        )

    @staticmethod
    def _split_domains(value: str) -> list[str]:
        return [part.strip() for part in value.split(",") if part.strip()]

    @property
    def allowed_domains(self) -> list[str]:
        return self._split_domains(self.web_search_allowed_domains)

    @property
    def blocked_domains(self) -> list[str]:
        return self._split_domains(self.web_search_blocked_domains)

    @property
    def browser_allowed_domains(self) -> list[str]:
        return [domain.lower() for domain in self._split_domains(
            self.local_browser_allowed_domains
        )]


@lru_cache
def get_settings() -> Settings:
    return Settings()
