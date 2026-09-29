import pytest

from autopost.content import load_json_object
from autopost.gemini import Gemini, GeminiError, extract_grounding
from fakes import FakeGeminiClient, FakeGeminiResponse, fake_gemini, grounded


def test_returns_parsed_value_and_grounding_sources():
    gemini, client = fake_gemini([grounded('{"a": 1}', ["https://x.com/1", "https://x.com/1", "https://y.com/2"])])
    data, reply = gemini.generate("prompt", load_json_object, grounded=True)
    assert data == {"a": 1}
    assert [s.url for s in reply.sources] == ["https://x.com/1", "https://y.com/2"]  # 중복 제거
    assert reply.queries == ("검색어",)
    config = client.models.calls[0]["config"]
    assert config.tools and config.tools[0].google_search is not None
    assert config.response_mime_type is None  # 검색 도구와 JSON 강제 출력은 함께 쓰지 않음


def test_json_mode_without_tools_forces_json_mime():
    gemini, client = fake_gemini(['{"a": 1}'])
    gemini.generate("prompt", load_json_object)
    config = client.models.calls[0]["config"]
    assert config.response_mime_type == "application/json" and not config.tools


def test_url_context_tool_is_added():
    gemini, client = fake_gemini(['{"a": 1}'])
    gemini.generate("prompt", load_json_object, grounded=True, url_context=True)
    tools = client.models.calls[0]["config"].tools
    assert tools[1].url_context is not None


def test_falls_back_to_next_model_on_error_or_bad_format():
    gemini, client = fake_gemini([RuntimeError("503"), "not json", '{"ok": true}'], models=("a", "b", "c"))
    data, reply = gemini.generate("p", load_json_object)
    assert data == {"ok": True} and reply.model == "c"
    assert [c["model"] for c in client.models.calls] == ["a", "b", "c"]


def test_raises_after_all_rounds_and_waits_between_rounds():
    waits = []
    client = FakeGeminiClient([RuntimeError("x")] * 4)
    gemini = Gemini("k", "a", ("b",), client=client, max_rounds=2, sleep=waits.append)
    with pytest.raises(GeminiError, match="4회 실패"):
        gemini.generate("p", load_json_object, label="테스트")
    assert waits == [30]


def test_model_with_exhausted_daily_quota_is_skipped_for_the_run():
    daily = RuntimeError("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    gemini, client = fake_gemini([daily, '{"a": 1}', '{"a": 2}'], models=("a", "b"))
    gemini.generate("p1", load_json_object)
    data, reply = gemini.generate("p2", load_json_object)
    assert data == {"a": 2} and reply.model == "b"
    assert [c["model"] for c in client.models.calls] == ["a", "b", "b"]  # 두 번째 호출은 'a' 를 건너뜀


def test_all_models_exhausted_still_tries_again():
    daily = RuntimeError("RESOURCE_EXHAUSTED PerDay")
    gemini, client = fake_gemini([daily, '{"ok": 1}'], models=("a",), rounds=2)
    assert gemini.generate("p", load_json_object)[0] == {"ok": 1}


def test_extract_grounding_handles_missing_metadata():
    assert extract_grounding(FakeGeminiResponse("x")) == ((), ())


def test_usage_counts_tokens_searches_and_discarded_replies():
    from autopost.gemini import Usage, usage_of

    class Meta:
        prompt_token_count = 1000
        tool_use_prompt_token_count = 500
        candidates_token_count = 300
        thoughts_token_count = 200

    good = grounded('{"ok": 1}', ["https://a"], queries=("q1", "q2"))
    good.usage_metadata = Meta()
    bad = grounded("not json", [], queries=("q3",))
    gemini, _ = fake_gemini([bad, good], models=("m1", "m2"))
    gemini.generate("p", load_json_object, grounded=True)
    usage = gemini.usage
    assert usage == Usage(requests=2, input_tokens=1500, output_tokens=500, searches=3)
    assert usage_of(object(), 0) == Usage(requests=1)
    assert (usage - Usage(requests=1, searches=1)).searches == 2
    cost = Usage(input_tokens=1_000_000, output_tokens=1_000_000, searches=1000).paid_cost()
    assert round(cost, 2) == 18.5
    assert "구글 검색 3회" in usage.line() and "유료 환산 약 $" in usage.line()
