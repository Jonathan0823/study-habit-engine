import asyncio
import sys
import types

from app.plan_generation import configured_gemini_plan_generator


def test_gemini_generator_uses_bounded_schema_output_and_only_supplied_sources(monkeypatch):
    observed = {}

    class FakeRunnable:
        async def ainvoke(self, messages):
            observed["messages"] = messages
            return {"goal_summary": "Learn Python", "topics": []}

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
    monkeypatch.setenv("AI_PLAN_MAX_OUTPUT_TOKENS", "2048")
    monkeypatch.setitem(
        sys.modules,
        "langchain_google_genai",
        types.SimpleNamespace(ChatGoogleGenerativeAI=FakeChatGoogleGenerativeAI),
    )

    generator = configured_gemini_plan_generator()
    result = asyncio.run(
        generator.generate(
            "Learn Python",
            {"weekly_minutes": 180},
            [
                {
                    "title": "Python tutorial",
                    "publisher": "Python docs",
                    "url": "https://docs.python.org/3/tutorial/",
                    "published_at": None,
                }
            ],
        )
    )

    assert result["goal_summary"] == "Learn Python"
    assert observed["model_options"] == {
        "model": "gemini-3.8-flash",
        "api_key": "test-key",
        "timeout": 20,
        "max_retries": 0,
        "max_output_tokens": 2048,
        "thinking_level": "minimal",
    }
    assert observed["method"] == "json_schema"
    assert observed["schema"]["additionalProperties"] is False
    assert "docs.python.org/3/tutorial/" in observed["messages"][1][1]
