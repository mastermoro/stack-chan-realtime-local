from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class DeviceState(StrEnum):
    READY = "ready"
    LISTENING = "listening"
    THINKING = "thinking"
    SEARCHING = "searching"
    SPEAKING = "speaking"
    ERROR = "error"


class HelloAudio(BaseModel):
    format: Literal["pcm16"] = "pcm16"
    sample_rate: Literal[24000] = 24000
    channels: Literal[1] = 1


class HelloMessage(BaseModel):
    type: Literal["hello"]
    protocol: Literal[1] = 1
    device_id: str = Field(min_length=1, max_length=128)
    audio: HelloAudio


class ControlMessage(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class Source(BaseModel):
    title: str = ""
    url: str


class SourcesMessage(BaseModel):
    type: Literal["sources"] = "sources"
    sources: list[Source]
