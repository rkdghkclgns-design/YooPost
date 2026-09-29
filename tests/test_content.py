import pytest

from autopost.content import (
    ContentError,
    Reference,
    inline_html,
    load_json_object,
    normalize_hashtags,
    parse_post,
    strip_inline,
)
from fakes import post_json


def test_parse_post_keeps_reference_blog_structure():
    content = parse_post(post_json())
    assert content.title.startswith("🤖 Gemini 3.8 Live")
    assert content.category == "AI 언어 모델"
    assert [s.heading for s in content.sections] == ["📰 Gemini 3.8 Live 등장 배경", "⚙️ 공식 스펙 정리"]
    kinds = [b.kind for s in content.sections for b in s.blocks]
    assert kinds == ["paragraph", "subheading", "info", "warning", "table", "code", "list", "paragraph"]
    table = content.sections[1].blocks[0]
    assert table.rows == (("공개일", "2026년 9월 24일"), ("개발사", ""))  # 모자란 칸은 빈 값으로
    assert [f.question for f in content.faq] == ["무료인가요?"]  # 빈 질문 제외
    assert content.hashtags == ("#Gemini", "#구글AI", "#AI", "#인공지능", "#AI뉴스")
    assert content.source_ids == ("S1", "S2")


def test_parse_post_rejects_missing_fields_and_thin_body():
    with pytest.raises(ContentError, match="title"):
        parse_post(post_json(title=""))
    with pytest.raises(ContentError, match="섹션"):
        parse_post(post_json(sections=[{"heading": "하나", "blocks": [{"type": "paragraph", "text": "a"}]}]))
    with pytest.raises(ContentError, match="JSON"):
        parse_post("not json")


def test_unknown_category_falls_back_to_default():
    assert parse_post(post_json(category="요리"), "AI 도구").category == "AI 도구"


def test_load_json_object_accepts_code_fence_and_surrounding_text():
    assert load_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert load_json_object('결과입니다: {"a": 2} 끝') == {"a": 2}
    with pytest.raises(ContentError, match="객체"):
        load_json_object("[1, 2]")


def test_inline_markup_helpers():
    assert strip_inline("**굵게** 와 `code`") == "굵게 와 code"
    assert inline_html("<b> **굵게** `x`\n다음") == "&lt;b&gt; <b>굵게</b> <code>x</code><br/>다음"

def test_plain_text_and_length_exclude_code_and_markup():
    content = parse_post(post_json()).with_references(
        [Reference("Google DeepMind", "2026, September 24", "Introducing", "https://deepmind.google/x")]
    )
    text = content.plain_text()
    assert "[한눈에 보기]" in text and "■ 📰 Gemini 3.8 Live 등장 배경" in text
    assert "💡 핵심 포인트 — gemini-3.8-live 모델 ID 를 씁니다." in text
    assert "```\npip install google-genai\n```" in text
    assert "Q1. 무료인가요?" in text
    assert "- Google DeepMind. (2026, September 24). Introducing. Retrieved from https://deepmind.google/x" in text
    assert "pip" not in content.claim_text()  # 코드블록은 사실 검증 대상이 아님
    assert content.text_length() > 100


def test_normalize_hashtags_limits_and_dedupes():
    tags = normalize_hashtags(["a b", "#A_B", "", "x" * 40], ("AI",), count=3)
    assert tags == ("#ab", "#A_B", "#" + "x" * 30)
