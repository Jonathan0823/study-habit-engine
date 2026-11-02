import asyncio

from app.source_search import TavilySourceSearcher


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "results": [
                {
                    "title": "Python tutorial",
                    "url": "https://docs.python.org/3/tutorial/",
                    "site_name": "Python documentation",
                    "published_date": "2026-01-01",
                    "content": "Discard this snippet.",
                },
                {
                    "title": "Lookalike host",
                    "url": "https://docs.python.org.attacker.example/tutorial/",
                    "site_name": "Attacker",
                    "published_date": None,
                    "content": "Discard this too.",
                },
            ]
        }


class FakeHttpClient:
    def __init__(self):
        self.request = None

    async def post(self, url, *, headers, json):
        self.request = {"url": url, "headers": headers, "json": json}
        return FakeResponse()


def test_tavily_search_restricts_domains_and_returns_only_safe_link_metadata():
    http_client = FakeHttpClient()
    searcher = TavilySourceSearcher(
        api_key="test-key",
        trusted_hosts={"docs.python.org"},
        http_client=http_client,
    )

    results = asyncio.run(searcher.search("Python tutorial"))

    assert results == [
        {
            "title": "Python tutorial",
            "publisher": "Python documentation",
            "url": "https://docs.python.org/3/tutorial/",
            "published_at": "2026-01-01",
        }
    ]
    assert http_client.request["url"] == "https://api.tavily.com/search"
    assert http_client.request["headers"]["Authorization"] == "Bearer test-key"
    assert http_client.request["json"]["include_domains"] == ["docs.python.org"]
    assert http_client.request["json"]["include_domains_mode"] == "restrict"
    assert http_client.request["json"]["search_depth"] == "basic"
    assert http_client.request["json"]["include_raw_content"] is False
    assert http_client.request["json"]["include_answer"] is False
