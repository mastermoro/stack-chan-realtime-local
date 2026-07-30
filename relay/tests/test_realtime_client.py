from app.config.settings import Settings
from app.realtime.client import FoundryRealtimeClient


def test_session_config_uses_marin_voice() -> None:
    client = FoundryRealtimeClient(Settings())

    assert client._session_config()["audio"]["output"]["voice"] == "marin"


def test_session_config_uses_patient_semantic_vad_by_default() -> None:
    client = FoundryRealtimeClient(Settings())

    assert client._session_config()["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "low",
        "create_response": True,
        "interrupt_response": False,
    }


def test_session_config_supports_tunable_server_vad_fallback() -> None:
    client = FoundryRealtimeClient(
        Settings(
            realtime_turn_detection_type="server_vad",
            realtime_vad_threshold=0.4,
            realtime_vad_prefix_padding_ms=400,
            realtime_vad_silence_duration_ms=1200,
        )
    )

    assert client._session_config()["audio"]["input"]["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.4,
        "prefix_padding_ms": 400,
        "silence_duration_ms": 1200,
        "create_response": True,
        "interrupt_response": False,
    }


def test_session_config_adds_registered_windows_tools() -> None:
    local_tool = {
        "type": "function",
        "name": "open_browser_url",
        "description": "Open a URL",
        "parameters": {"type": "object", "properties": {}},
    }
    client = FoundryRealtimeClient(Settings(), additional_tools=[local_tool])

    tools = client._session_config()["tools"]

    assert [tool["name"] for tool in tools] == [
        "search_web",
        "set_emotion",
        "open_browser_url",
    ]
