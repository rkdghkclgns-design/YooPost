import json

import pytest

from autopost.manual_command import (
    MAX_TOPIC, ManualCommand, fire_text, parse_command, request_id, split_request_id,
)


@pytest.mark.parametrize("text, category, topic", [
    ("슬랙에 에이전트 직원 만들기를 AI도구에 발행해줘", "AI 도구", "슬랙에 에이전트 직원 만들기"),
    ("슬랙에 에이전트 직원 만들기를 AI 도구에 발행해 줘", "AI 도구", "슬랙에 에이전트 직원 만들기"),
    ("AI 보안 카테고리로 MCP 서버 권한 탈취 사례 써줘", "AI 보안", "MCP 서버 권한 탈취 사례"),
    ("Claude Sonnet 5.5 사용 가이드 AI언어모델에 올려줘", "AI 언어 모델", "Claude Sonnet 5.5 사용 가이드"),
    ("공모주 카테고리에 엘리스그룹 상장 첫날 정리 발행", "AI 공모주 분석", "엘리스그룹 상장 첫날 정리"),
    ("논문: Mixture of Depths 분석 발행해줘", "AI 논문 분석", "Mixture of Depths 분석"),
])
def test_single_post_with_topic_and_category(text, category, topic):
    command = parse_command(text)
    assert (command.kind, command.category, command.topic) == ("single", category, topic)


def test_category_marker_beats_category_words_inside_the_topic():
    command = parse_command("AI 보안 사고 사례 모음을 뉴스에 발행해줘")
    assert (command.category, command.topic) == ("AI 뉴스 리뷰", "AI 보안 사고 사례 모음")
    command = parse_command("로봇 도구 비교 발행해줘")  # '도구' 뒤에 '에·로·카테고리' 가 없으면 카테고리가 아니다
    assert (command.category, command.topic) == ("", "로봇 도구 비교")


def test_all_word_inside_a_topic_is_a_single_post():
    command = parse_command("OpenAI 전체 모델 비교 발행해줘")
    assert (command.kind, command.topic) == ("single", "OpenAI 전체 모델 비교")


@pytest.mark.parametrize("text", ["고마워", "테스트", "ㅋㅋ 잘 되네", "ok", "좋아요 👍", "이거 왜 안 돼?", "AI 보안 한 편 더"])
def test_chit_chat_without_a_writing_verb_is_not_an_order(text):
    """끝에 '발행해줘·써줘·올려줘' 가 없으면 지시가 아니다 — 잡담으로 유료 루틴이 돌지 않게."""
    command = parse_command(text)
    assert command.kind == "invalid" and "발행해줘" in command.error


@pytest.mark.parametrize("text", [
    "7개 카테고리마다 새 글 1편씩 발행해줘", "카테고리마다 한 편씩 써줘", "카테고리별로 한 편씩 발행해줘",
    "카테고리별로 1편씩 발행해줘", "전체 카테고리 새 글 발행해줘", "7개 카테고리 전부 새로 발행해줘",
    "모든 카테고리에 새 글 발행해줘", "전체 다시 발행해줘", "`전체 새로운 포스트로 재발행`", "각 카테고리 새 글 올려줘",
])
def test_natural_all_category_phrasings(text):
    assert parse_command(text).kind == "all"


@pytest.mark.parametrize("text, category, topic", [
    ("논문 쓰는 AI 도구 비교 발행해줘", "", "논문 쓰는 AI 도구 비교"),
    ("코딩 도구로 쓰는 Claude Code 소개 발행해줘", "", "코딩 도구로 쓰는 Claude Code 소개"),
    ("LLM으로 보안 점검 자동화 발행해줘", "", "LLM으로 보안 점검 자동화"),
    ("보안에 강한 로컬 LLM 추천 발행해줘", "", "보안에 강한 로컬 LLM 추천"),
    ("뉴스로 보는 AI 트렌드 발행해줘", "", "뉴스로 보는 AI 트렌드"),
    ("AI 보안 취약점 사례 정리 발행해줘", "", "AI 보안 취약점 사례 정리"),
    ("AI 뉴스 리뷰 자동화 봇 만들기 발행해줘", "", "AI 뉴스 리뷰 자동화 봇 만들기"),
    ("오픈소스 AI 인사이트 정리 발행해줘", "", "오픈소스 AI 인사이트 정리"),
    ("LLM으로 보안 점검 자동화를 AI 보안에 발행해줘", "AI 보안", "LLM으로 보안 점검 자동화"),
])
def test_category_words_inside_a_topic_stay_in_the_topic(text, category, topic):
    command = parse_command(text)
    assert (command.kind, command.category, command.topic) == ("single", category, topic)


def test_parsing_stays_fast_on_pathological_input():
    import time

    started = time.perf_counter()
    for text in ("한편" * 200 + "X 발행해줘", "새로" * 200 + "X 발행해줘", "포스트로 " * 100 + "X 발행해줘"):
        parse_command(text)
    assert time.perf_counter() - started < 1.0


def test_single_without_category_lets_the_writer_choose():
    command = parse_command("구글 Gemini 4 출시 소식 발행해줘")
    assert (command.kind, command.category, command.topic) == ("single", "", "구글 Gemini 4 출시 소식")


def test_category_without_topic_means_a_fresh_topic_in_that_category():
    command = parse_command("AI 인사이트 한 편 더 발행해줘")
    assert (command.kind, command.category, command.topic) == ("single", "AI 인사이트", "")


@pytest.mark.parametrize("text", [
    "전체 새로운 포스트로 재발행", "전체 발행", "카테고리별로 새 글 써줘", "모든 카테고리 새로 발행해줘", "7편 전부 새로 써줘",
])
def test_all_categories(text):
    command = parse_command(text)
    assert (command.kind, command.category, command.topic) == ("all", "", "")


@pytest.mark.parametrize("text", ["도움말", "help", "사용법", "?", "  "])
def test_help(text):
    assert parse_command(text).kind == "help"


def test_invalid_when_nothing_to_write():
    command = parse_command("발행해줘")
    assert command.kind == "invalid" and command.error


def test_slack_markup_is_removed_and_topic_is_capped():
    command = parse_command("<@U0C4PLGBNF9> <https://slack.com/intl/ko-kr/ai-agents|슬랙 AI 에이전트> 소개를 *AI 도구*에 발행해줘")
    assert command.category == "AI 도구"
    assert command.topic == "슬랙 AI 에이전트 소개"
    long = parse_command("가" * 500 + " AI 도구에 발행해줘")
    assert len(long.topic) <= MAX_TOPIC


def test_request_id_roundtrip_and_validation():
    rid = request_id("C0ABC123", "1727650000.123456")
    assert rid == "C0ABC123-1727650000123456"
    assert split_request_id(rid) == ("C0ABC123", "1727650000.123456")
    assert split_request_id("../../etc") is None
    assert split_request_id("C0ABC-12") is None


def test_fire_text_carries_only_id_kind_and_category():
    """주제는 보내지 않는다 — 루틴은 Actions 가 원고 브랜치에 남긴 요청 기록에서 주제를 읽는다."""
    command = ManualCommand("single", "AI 도구", "슬랙 에이전트")
    payload = json.loads(fire_text(command, "C0ABC123-1727650000123456"))
    assert payload == {"yoopost_manual_request": 1, "id": "C0ABC123-1727650000123456", "kind": "single",
                       "category": "AI 도구"}
    with pytest.raises(ValueError):
        fire_text(ManualCommand("help"), "C0ABC123-1727650000123456")
