import json

import pytest

from pathlib import Path

from autopost.links import LinkCheck
from autopost.research import (
    ImageCandidate,
    QuoteImage,
    Researcher,
    ResearchError,
    build_research_prompt,
    pick_sources,
    reference_date,
)
from fakes import fake_gemini, grounded, make_sheet, make_topic

OFFICIAL = "https://deepmind.google/blog/gemini-3-8-live/"
NEWS = "https://www.aitimes.com/news/1"
BLOCKED = "https://openai.com/blocked"
FAKE = "https://made-up.example/404"
OFFTOPIC = "https://random.com/page"


def research_json(facts=12):
    return json.dumps({
        "summary": "요약",
        "sources": [
            {"id": "S1", "title": "Introducing", "url": OFFICIAL, "publisher": "Google DeepMind",
             "published": "2026-09-24", "official": True},
            {"id": "S2", "title": "", "url": NEWS, "publisher": "", "published": ""},
            {"id": "S3", "title": "Blocked", "url": BLOCKED, "publisher": "OpenAI"},
            {"id": "S4", "title": "Fake", "url": FAKE, "publisher": "?"},
            {"id": "S5", "title": "Off", "url": OFFTOPIC, "publisher": "?"},
            {"id": "S6", "title": "bad", "url": "not-a-url"},
        ],
        "facts": [{"statement": f"사실 {i}", "source_ids": ["S1", "S4"]} for i in range(facts)]
        + [{"statement": "가짜 출처만 있는 사실", "source_ids": ["S4"]},
           {"statement": "뉴스 사실", "source_ids": ["s2"]},
           {"statement": "차단된 공식 사실", "source_ids": ["S3"]}],
        "unknowns": ["가격", ""],
    })


CHECKS = {
    OFFICIAL: LinkCheck(OFFICIAL, 200, "Introducing Gemini 3.8 Live", True),
    NEWS: LinkCheck(NEWS, 200, "AI타임스 기사", True),
    BLOCKED: LinkCheck(BLOCKED, 403),
    FAKE: LinkCheck(FAKE, 404),
    OFFTOPIC: LinkCheck(OFFTOPIC, 200, "Random", False),
}


def researcher(responses, min_sources=2):
    gemini, client = fake_gemini(responses)
    return Researcher(
        gemini, session=None, min_sources=min_sources,
        checker=lambda url, keywords, session: CHECKS[url],
        resolver=lambda url, session: url.replace("https://redirect/", "https://"),
    ), client


def test_keeps_only_verified_sources_and_their_facts():
    reply = grounded(research_json(), ["https://redirect/openai.com/blocked"])
    res, client = researcher([reply])
    sheet = res.research(make_topic(), "2026-09-29")
    assert [s.id for s in sheet.sources] == ["S1", "S2", "S3"]  # 404·주제 무관 출처 제외
    assert sheet.source("S3").grounded  # 봇 차단이지만 검색 근거로 확인된 주소
    assert sheet.source("S2").publisher == "aitimes.com" and sheet.source("S2").title == "AI타임스 기사"
    assert all("S4" not in f.source_ids for f in sheet.facts)
    assert "가짜 출처만 있는 사실" not in [f.statement for f in sheet.facts]
    assert sheet.unknowns == ("가격",) and sheet.as_of == "2026-09-29"
    config = client.models.calls[0]["config"]
    assert len(config.tools) == 2  # 구글 검색 + 출발 자료 읽기


def test_blocked_source_without_grounding_is_dropped():
    res, _ = researcher([grounded(research_json(), [])])
    sheet = res.research(make_topic(source_url=""), "2026-09-29")
    assert "S3" not in [s.id for s in sheet.sources]


def test_too_few_facts_or_sources_raise():
    res, _ = researcher([grounded(research_json(facts=3), [])])
    with pytest.raises(ResearchError, match="사실"):
        res.research(make_topic(), "2026-09-29")
    res, _ = researcher([grounded(research_json(), [])], min_sources=5)
    with pytest.raises(ResearchError, match="출처"):
        res.research(make_topic(), "2026-09-29")


def test_sheet_prompt_and_markdown():
    sheet = make_sheet()
    prompt = sheet.as_prompt()
    assert "[S1] Google DeepMind — Introducing Gemini 3.8 Live (2026-09-24) [공식]" in prompt
    assert "- 가격" in prompt
    assert "## 확인된 사실" in sheet.markdown() and "(공식) Google DeepMind" in sheet.markdown()
    assert "출발 자료: https://deepmind.google" in build_research_prompt(make_topic(), "2026-09-29")


def test_reference_date_and_pick_sources():
    assert reference_date("2026-09-26", "2026") == "2026, September 26"
    assert reference_date("", "2026") == "2026"
    assert reference_date("2025", "2026") == "2025"
    sheet = make_sheet()
    assert [s.id for s in pick_sources(sheet, ["S2"])] == ["S2"]
    assert [s.id for s in pick_sources(sheet, [])] == ["S1", "S2"]  # 인용 표시가 없으면 전체 (공식 먼저)


# ---------------------------------------------------------------- 인용 이미지 후보 (구글 검색 조사)
def test_search_research_also_returns_quote_image_pages():
    data = json.loads(research_json())
    data["images"] = [{"id": "I1", "image_url": "", "page_url": OFFICIAL, "caption": "Gemini 3.8 Live 구조도",
                       "credit": "Google DeepMind"},
                      {"id": "I2", "image_url": "bad", "page_url": "bad", "caption": "x"}]
    res, client = researcher([grounded(json.dumps(data, ensure_ascii=False), [])])
    sheet = res.research(make_topic(), "2026-09-29")
    assert sheet.engine == "구글 검색" and [c.id for c in sheet.image_candidates] == ["I1"]
    assert sheet.image_candidates[0].page_url == OFFICIAL and sheet.image_candidates[0].image_url == ""
    prompt = client.models.calls[0]["contents"]
    assert "[인용 이미지]" in prompt and "page_url" in prompt
    assert "조사 방식: 구글 검색" in sheet.markdown() and "## 인용 이미지" in sheet.markdown()


def test_images_are_listed_only_for_the_writer(tmp_path):
    image = QuoteImage("I1", "구조도", "NVIDIA", "https://n/p", Path(tmp_path / "a.jpg"))
    sheet = make_sheet(images=(image,), image_candidates=(
        ImageCandidate("I1", "https://n/a.png", "https://n/p", "구조도"),
        ImageCandidate("I2", "https://n/b.png", "https://n/p", "실패한 이미지")))
    assert "[인용 이미지" not in sheet.as_prompt()
    assert "[I1] 구조도 (출처: NVIDIA)" in sheet.as_prompt(include_images=True)
    assert "(없음)" in make_sheet().as_prompt(include_images=True)
    markdown = sheet.markdown()
    assert "✅ 사용 가능 구조도" in markdown and "❌ 확인 실패 실패한 이미지" in markdown
