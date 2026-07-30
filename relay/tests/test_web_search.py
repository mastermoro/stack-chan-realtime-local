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
