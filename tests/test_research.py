import json

import pytest

from pathlib import Path

from autopost.deep_research import DeepResearchReport
from autopost.links import LinkCheck
from autopost.research import (
    ImageCandidate,
    QuoteImage,
    Researcher,
    ResearchError,
    build_research_prompt,
    extract_report_json,
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


# ---------------------------------------------------------------- 구글 딥리서치
class StubDeep:
    def __init__(self, text="", error=None, cited=()):
        self.text, self.error, self.cited = text, error, tuple(cited)
        self.prompts = []

    def run(self, prompt, progress=None):
        self.prompts.append(prompt)
        if progress is not None:
            progress("구글 딥리서치 조사 중 — 2분 경과")
        if self.error:
            raise self.error
        return DeepResearchReport(self.text, self.cited, "int-1", 9.8)


def deep_researcher(deep, responses=(), min_sources=2):
    gemini, client = fake_gemini(list(responses))
    return Researcher(
        gemini, session=None, min_sources=min_sources,
        checker=lambda url, keywords, session: CHECKS[url],
        resolver=lambda url, session: url.replace("https://redirect/", "https://"),
        deep=deep,
    ), client


def report_with_json(images=()):
    data = json.loads(research_json())
    data["images"] = list(images)
    return ("# 조사 보고서\n```python\nprint({'not': 'json'})\n```\n본문 [cite: S1]\n\n```json\n"
            + json.dumps(data, ensure_ascii=False) + "\n```\n")


def test_deep_research_report_json_is_used_directly():
    images = [{"id": "I1", "image_url": "https://deepmind.google/i.png", "page_url": OFFICIAL,
               "caption": "Gemini 3.8 Live 구조도", "credit": "Google DeepMind"},
              {"id": "I2", "image_url": "bad", "page_url": "bad", "caption": "x"}]
    deep = StubDeep(report_with_json(images), cited=["https://redirect/openai.com/blocked"])
    res, client = deep_researcher(deep)
    notes = []
    sheet = res.research(make_topic(), "2026-09-29", progress=notes.append)
    assert sheet.engine == "구글 딥리서치" and client.models.calls == []  # 옮겨 적기 불필요
    assert [s.id for s in sheet.sources] == ["S1", "S2", "S3"]  # 차단된 주소도 딥리서치 인용이면 인정
    assert [c.id for c in sheet.image_candidates] == ["I1"]
    assert notes == ["구글 딥리서치 조사 중 — 2분 경과"]
    prompt = deep.prompts[0]
    assert "출발 자료: https://deepmind.google" in prompt and "인용 이미지" in prompt and "```json" in prompt
    assert "조사 방식: 구글 딥리서치" in sheet.markdown() and "## 인용 이미지" in sheet.markdown()


def test_report_without_json_is_transcribed_without_search():
    deep = StubDeep("# 보고서만 있고 JSON 은 없음 [cite: S1]")
    res, client = deep_researcher(deep, [research_json()])
    sheet = res.research(make_topic(), "2026-09-29")
    assert sheet.engine == "구글 딥리서치" and len(sheet.sources) == 2
    call = client.models.calls[0]
    assert "보고서만 있고" in call["contents"] and call["config"].tools is None


def test_deep_research_failure_falls_back_to_google_search():
    deep = StubDeep(error=RuntimeError("429 RESOURCE_EXHAUSTED"))
    res, client = deep_researcher(deep, [grounded(research_json(), [])])
    notes = []
    sheet = res.research(make_topic(), "2026-09-29", progress=notes.append)
    assert sheet.engine == "구글 검색 (딥리서치 대체)" and "구글 검색 조사로 대체" in notes[-1]
    assert len(client.models.calls[0]["config"].tools) == 2


def test_extract_report_json_prefers_the_last_json_block():
    text = '```json\n{"a": 1}\n```\n중간\n```json\n{"b": 2}\n```'
    assert extract_report_json(text) == {"b": 2}
    assert extract_report_json('앞 설명 {"c": 3} 뒤') == {"c": 3}


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
