from app.tools.web_search import WebSearchExecutor


class FakeResponse:
    output_text = "検索に基づく回答"

    def model_dump(self):
        return {
            "output": [
                {"type": "web_search_call", "status": "completed"},
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": self.output_text,
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://learn.microsoft.com/example",
                                    "title": "Example",
                                },
                                {
                                    "type": "url_citation",
                                    "url": "https://learn.microsoft.com/example",
                                    "title": "Duplicate",
                                },
                            ],
                        }
                    ],
                },
            ]
        }


def test_parse_response_extracts_unique_sources() -> None:
    result = WebSearchExecutor.parse_response(FakeResponse())
    assert result.answer == "検索に基づく回答"
    assert len(result.sources) == 1
    assert result.sources[0].title == "Example"


class TypedValue:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


def test_parse_response_extracts_current_action_sources_without_model_dump() -> None:
    response = TypedValue(
        output_text="検索結果",
        output=[
            TypedValue(
                type="web_search_call",
                action=TypedValue(
                    sources=[
                        TypedValue(type="url", url="https://example.com/current"),
                        TypedValue(type="url", url="https://example.com/current"),
                    ]
                ),
            ),
            TypedValue(type="message", content=[]),
        ],
    )

    result = WebSearchExecutor.parse_response(response)

    assert result.answer == "検索結果"
    assert [source.model_dump() for source in result.sources] == [
        {"title": "example.com", "url": "https://example.com/current"}
    ]
