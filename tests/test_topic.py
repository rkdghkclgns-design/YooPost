import json
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from autopost.feeds import Candidate
from autopost.history import HistoryEntry, PostHistory
from autopost.links import LinkCheck
from autopost.topic import TopicError, TopicSelector, build_pick_prompt
from fakes import fake_gemini, grounded

NOW = datetime(2026, 9, 29, 1, tzinfo=timezone.utc)
CANDIDATES = [
    Candidate("Introducing Gemini 3.8 Live", "https://deepmind.google/blog/x", "Google DeepMind", NOW, "live", True),
    Candidate("Claude Sonnet 5.5 공개", "https://www.aitimes.com/1", "AI타임스", NOW, "소네트"),
]


LIVE = {"https://openai.com/news/gpt-6": LinkCheck("https://openai.com/news/gpt-6", 200, "GPT-6", True),
        "https://blocked.com/x": LinkCheck("https://blocked.com/x", 403)}


def selector(responses, candidates=CANDIDATES, models=("m1",)):
    gemini, client = fake_gemini(responses, models=models)
    checker = lambda url, keywords, session: LIVE.get(url, LinkCheck(url, 404))  # noqa: E731
    return TopicSelector(gemini, session=None, collect=lambda *a, **k: list(candidates), checker=checker), client


def test_picks_candidate_chosen_by_ai_and_skips_already_posted():
    history = PostHistory((HistoryEntry("2026-09-28", "t", "Introducing Gemini 3.8 Live", "AI 언어 모델",
                                        "https://deepmind.google/blog/x", MappingProxyType({})),))
    pick = json.dumps({"index": 0, "category": "AI 언어 모델", "angle": "스펙 총정리", "keywords": ["Claude Sonnet 5.5"]})
    topics, client = selector([pick])
    topic, seen = topics.pick(history, NOW)
    assert topic.title == "Claude Sonnet 5.5 공개" and topic.source_url == "https://www.aitimes.com/1"
    assert seen == 2 and topic.keywords == ("Claude Sonnet 5.5",)
    assert "Gemini 3.8" not in client.models.calls[0]["contents"].split("[후보]")[1]  # 이미 쓴 주제는 후보에서 제외


def test_invalid_index_tries_next_model_and_unknown_category_defaults():
    topics, _ = selector([json.dumps({"index": 9}), json.dumps({"index": 1, "category": "요리"})], models=("a", "b"))
    topic, _ = topics.pick(PostHistory(), NOW)
    assert topic.title == "Claude Sonnet 5.5 공개" and topic.category == "AI 뉴스 리뷰"
    assert topic.keywords == ("Claude Sonnet 5.5 공개",)


def test_requested_topic_uses_grounded_search():
    reply = json.dumps({"ok": True, "title": "GPT-6 공개", "category": "AI 언어 모델", "keywords": ["GPT-6"],
                        "source_url": "https://openai.com/news/gpt-6", "source_name": "OpenAI", "official": True})
    topics, client = selector([grounded(reply)])
    topic, seen = topics.pick(PostHistory(), NOW, requested="gpt6")
    assert topic.requested and topic.official and seen == 0
    assert client.models.calls[0]["config"].tools


def test_ai_given_source_urls_are_checked_before_use():
    dead = json.dumps({"ok": True, "title": "GPT-6", "source_url": "https://made-up.example/gpt6"})
    topics, _ = selector([grounded(dead)])
    assert topics.pick(PostHistory(), NOW, requested="gpt6")[0].source_url == ""  # 없는 주소는 비움
    blocked = json.dumps({"ok": True, "title": "GPT-6", "source_url": "https://blocked.com/x"})
    topics, _ = selector([grounded(blocked)])
    assert topics.pick(PostHistory(), NOW, requested="gpt6")[0].source_url == "https://blocked.com/x"


def test_requested_topic_rejected_when_not_verifiable():
    topics, _ = selector([grounded(json.dumps({"ok": False}))])
    with pytest.raises(TopicError, match="확인하지 못했습니다"):
        topics.pick(PostHistory(), NOW, requested="요리 레시피")


def test_falls_back_to_web_search_when_no_candidates():
    reply = json.dumps({"title": "Llama 5 공개", "category": "AI 언어 모델", "source_url": "not-a-url"})
    topics, _ = selector([grounded(reply)], candidates=[])
    topic, seen = topics.pick(PostHistory(), NOW)
    assert topic.title == "Llama 5 공개" and topic.source_url == "" and seen == 0


def test_search_fallback_rejects_duplicate():
    history = PostHistory((HistoryEntry("2026-09-28", "Llama 5 공개", "Llama 5 공개", "AI 언어 모델"),))
    topics, _ = selector([grounded(json.dumps({"title": "Llama 5 공개"}))], candidates=[])
    with pytest.raises(TopicError, match="새 주제"):
        topics.pick(history, NOW)


def test_pick_prompt_lists_recent_titles_and_categories():
    prompt = build_pick_prompt(CANDIDATES, ["지난 글"], "2026-09-29")
    assert "- 지난 글" in prompt and "0. [공식·Google DeepMind" in prompt and "AI 논문 분석" in prompt
