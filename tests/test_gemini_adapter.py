import asyncio
import sys
import types

from app.ranking import configured_gemini_ranker


def test_gemini_adapter_is_disabled_without_free_tier_acknowledgement(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_FREE_TIER_ACKNOWLEDGED", raising=False)

    assert configured_gemini_ranker() is None


def test_gemini_adapter_refuses_model_without_a_pinned_free_tier_price(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_FREE_TIER_ACKNOWLEDGED", "true")
    monkeypatch.setenv("GEMINI_MODEL", "unpriced-model")

    assert configured_gemini_ranker() is None


def test_gemini_adapter_uses_schema_and_one_bounded_nonretrying_call(monkeypatch):
    observed = {}

    class FakeRunnable:
        async def ainvoke(self, messages):
            observed["messages"] = messages
            return {"ranked_topics": [{"topic_id": "arrays", "reason_code": "NO_PREREQUISITES"}]}

    class FakeChatGoogleGenerativeAI:
        def __init__(self, **kwargs):
            observed["model_options"] = kwargs

        def with_structured_output(self, schema, *, method):
            observed["schema"] = schema
            observed["method"] = method
            return FakeRunnable()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_FREE_TIER_ACKNOWLEDGED", "true")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setitem(
        sys.modules,
        "langchain_google_genai",
        types.SimpleNamespace(ChatGoogleGenerativeAI=FakeChatGoogleGenerativeAI),
    )

    ranker = configured_gemini_ranker()
    result = asyncio.run(
        ranker(
            [
                {
                    "topic_id": "arrays",
                    "title": "Arrays",
                    "description": "Learn arrays",
                    "position": 0,
                    "estimate_minutes": 30,
                    "started_minutes": 0,
                    "prerequisites": [],
                    "completed_prerequisites": [],
                    "resources": ["must not be sent"],
                }
            ]
        )
    )

    assert result["ranked_topics"][0]["topic_id"] == "arrays"
    assert observed["model_options"] == {
        "model": "gemini-3.8-flash",
        "api_key": "test-key",
        "timeout": 10,
        "max_retries": 0,
        "max_output_tokens": 512,
        "thinking_level": "minimal",
    }
    assert observed["method"] == "json_schema"
    assert observed["schema"]["additionalProperties"] is False
    human_message = observed["messages"][1][1]
    assert "must not be sent" not in human_message
    assert "arrays" in human_message
