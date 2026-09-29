import json

from autopost.content import parse_post
from autopost.style_profile import StyleGuide
from autopost.writer import PostWriter, build_write_prompt, content_to_json, ensure_signature
from fakes import fake_gemini, make_sheet, post_dict, post_json

STYLE = StyleGuide(total_posts=950, target_chars=9000, headings=("📰 등장 배경", "⚙️ 공식 스펙 정리"))


def long_post(**overrides):
    body = "구글이 발표한 내용을 자세히 설명합니다. " * 30
    data = post_dict(**overrides)
    data["sections"][0]["blocks"].append({"type": "paragraph", "text": body})
    return json.dumps(data, ensure_ascii=False)


def test_prompt_contains_style_rules_facts_and_schema():
    prompt = build_write_prompt(make_sheet(), STYLE, "AI백과사전", "2026-09-29", 3000)
    assert "참고 블로그 전체 950개 게시물 분석 결과" in prompt
    assert "  - 📰 등장 배경" in prompt and "[확인된 사실]" in prompt and "[S1]" in prompt
    assert "3,000~9,000자" in prompt and '"source_ids"' in prompt
    assert "다시 찾아뵙겠습니다" in prompt


def test_write_adds_closing_signature():
    gemini, _ = fake_gemini([long_post()])
    content = PostWriter(gemini, "AI백과사전", min_chars=100).write(make_sheet(), STYLE, "2026-09-29")
    assert content.closing.endswith("다음 시간에 더 유익한 정보를 가지고 다시 찾아뵙겠습니다. 감사합니다.")
    assert ensure_signature("… 다시 찾아뵙겠습니다. 감사합니다.") == "… 다시 찾아뵙겠습니다. 감사합니다."


def test_short_draft_is_requested_again_and_longer_one_wins():
    gemini, client = fake_gemini([post_json(), long_post()])
    content = PostWriter(gemini, "AI백과사전", min_chars=500).write(make_sheet(), STYLE, "2026-09-29")
    assert content.text_length() >= 500
    assert "너무 짧았습니다" in client.models.calls[1]["contents"]


def test_short_retry_keeps_first_when_retry_is_shorter():
    gemini, _ = fake_gemini([long_post(), post_json()])
    content = PostWriter(gemini, "AI백과사전", min_chars=100000).write(make_sheet(), STYLE, "2026-09-29")
    assert content.text_length() > parse_post(post_json()).text_length()


def test_revise_sends_issues_and_current_json():
    gemini, client = fake_gemini([post_json(title="🤖 고친 제목")])
    writer = PostWriter(gemini, "AI백과사전", min_chars=100)
    original = parse_post(post_json())
    revised = writer.revise(original, make_sheet(), [{"claim": "가격은 무료", "verdict": "contradicted"}])
    assert revised.title == "🤖 고친 제목"
    prompt = client.models.calls[0]["contents"]
    assert "가격은 무료" in prompt and '"title": "🤖 Gemini 3.8 Live' in prompt


def test_content_to_json_roundtrip():
    original = parse_post(post_json())
    assert parse_post(content_to_json(original)) == original
