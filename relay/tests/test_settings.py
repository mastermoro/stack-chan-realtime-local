from app.config.settings import Settings


def test_endpoint_conversion() -> None:
    settings = Settings(
        azure_openai_endpoint="https://example.openai.azure.com/",
        device_tokens_json={},
    )
    assert settings.realtime_websocket_base_url == "wss://example.openai.azure.com/openai/v1"
    assert settings.responses_base_url == "https://example.openai.azure.com/openai/v1/"


def test_foundry_configured_rejects_placeholder_endpoint() -> None:
    settings = Settings(
        azure_openai_endpoint="https://YOUR-RESOURCE.openai.azure.com",
        azure_openai_realtime_deployment="gpt-realtime-2.1",
        azure_openai_responses_deployment="gpt-5.6-terra",
    )

    assert not settings.foundry_configured


def test_foundry_configured_accepts_resource_endpoint() -> None:
    settings = Settings(
        azure_openai_endpoint="https://my-resource.openai.azure.com",
        azure_openai_realtime_deployment="gpt-realtime-2.1",
        azure_openai_responses_deployment="gpt-5.6-terra",
    )

    assert settings.foundry_configured


def test_web_search_timeout_is_bounded_and_configurable() -> None:
    assert Settings(web_search_timeout_seconds=75).web_search_timeout_seconds == 75


def test_browser_allowed_domains_are_normalized() -> None:
    settings = Settings(local_browser_allowed_domains=" Example.COM, localhost ")

    assert settings.browser_allowed_domains == ["example.com", "localhost"]
