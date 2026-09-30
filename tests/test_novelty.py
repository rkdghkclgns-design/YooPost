"""기존 글과 겹치지 않기 · 최신 이슈만 (2026-10-01 사용자 지시)."""
from datetime import date

import pytest

from autopost.freshness import (
    folder_date, freshness_problems, issue_sites, page_date_check, page_published, window_days,
)
from autopost.history import HistoryEntry, HistoryUnavailable, PostHistory, combined, load_history, versions
from autopost.research import Source

TODAY = date(2026, 10, 1)


def entry(title, topic="", url="", sources=(), where="발행한 글"):
    return HistoryEntry("2026-09-29", title, topic or title, "AI 언어 모델", url, sources=tuple(sources), where=where)


def src(url, published="", official=False, sid="S1"):
    return Source(id=sid, title="t", url=url, publisher="p", published=published, official=official)


# ------------------------------------------------------------------ 겹침
def test_versions_ignore_dates_amounts_and_model_sizes():
    assert versions("Introducing GPT-6.1 Sol (OpenAI, 2026-09-29)") == {"6.1"}
    assert versions("Grok 5") == {"5"} and versions("DeepSeek-V4.1-Flash") == {"4.1"}
    assert versions("Claude Code 2.1.280–2.1.285 (2026-09-22~29)") == {"2.1.280", "2.1.285"}
    assert versions("엘리스그룹 정정 증권신고서(2026-09-29) 신주 2,180,000주 희망공모가 7,600~9,000원 250만주") == set()
    assert versions("Epoch AI 2026년 9월 분석 4편") == set() and versions("Llama 4 Scout 17B") == {"4"}
    # 퍼센트·금액·점수·날짜·Top/Day 번호는 버전이 아니다 (리뷰 dedup-1)
    for text in ("세계 18.8%", "입력 $0.15", "$11.6 Billion", "23.5억", "CVSS 9.8", "Top 10", "Day 2", "Pro 500",
                 "iOS 26", "수요예측 10/13~19", "AIME 12.5%p 향상", "Qwen3.5 1.5B"):
        assert versions(text) in (set(), {"3.5"}), text


def test_numbers_in_titles_never_switch_off_the_same_source_check():
    arxiv = "https://arxiv.org/abs/2609.35426"
    history = PostHistory((entry("🧠 Frontier Learning 논문 분석: Dice 71.8% vs 33.3%",
                                 "Frontier Learning: Training LLM Reasoners (arXiv 2609.35426)", arxiv),))
    found = history.find_duplicate("Frontier Learning: Training LLM Reasoners (arXiv 2609.35426)", arxiv,
                                   title="🧠 Frontier Learning 논문 분석: AIME 12.5%p 향상 총정리")
    assert found is not None and "같은 원문" in found[1]


def test_words_are_counted_once_and_generic_words_do_not_count():
    from autopost.history import _shared_count, _subject_words

    assert _shared_count(frozenset({"code", "codex", "claude"}), frozenset({"code", "claude"})) == 2
    assert _shared_count(frozenset({"엘리스그룹의"}), frozenset({"엘리스그룹"})) == 1  # 한글은 조사가 붙어도 같은 말
    assert _subject_words("AI 에이전트 논문 arxiv agent security in to of") == frozenset()
    changelog = "https://code.claude.com/docs/en/changelog"
    history = PostHistory((entry("🛠 Claude Code 2.1.280~2.1.285 총정리", "Claude Code changelog 2.1.280–2.1.285",
                                 changelog),))
    found = history.find_duplicate("GitSpawn: A Single Flaw Lets Untrusted Repos Run Code in Claude Code, Codex and more",
                                   "https://manifold.security/blog/ai-coding-agents-git-hijack",
                                   title="🔒 GitSpawn 총정리: 저장소 .git 설정 하나로 AI 코딩 에이전트 7종 탈취",
                                   sources=["https://manifold.security/blog/ai-coding-agents-git-hijack", changelog])
    assert found is None  # 패치 버전 근거로 변경 기록을 인용했을 뿐 — 다른 소식 (리뷰 dedup-2)


def test_the_same_news_through_other_outlets_is_caught():
    story = "https://dataconomy.com/2026/09/29/amd-acquires-world-labs"
    history = PostHistory((entry("AMD acquires World Labs for $8.2 billion in all-stock deal", url="https://ir.amd.com/news/1",
                                 sources=["https://ir.amd.com/news/1", story, "https://www.amd.com/en/docs/pricing"]),))
    found = history.find_duplicate("World Labs agrees to all-stock acquisition by AMD", "https://therundown.ai/world-labs",
                                   sources=["https://therundown.ai/world-labs", story])
    assert found is not None  # 같은 기사를 함께 인용·고유한 말이 대부분 같음 (리뷰 dedup-3)
    docs_only = PostHistory((entry("Gemini 3.8 Flash 출시", url="https://blog.google/gemini-3-8-flash",
                                   sources=["https://ai.google.dev/gemini-api/docs/models"]),))
    assert docs_only.find_duplicate("Gemini Live 음성 대화 업데이트", "https://blog.google/gemini-live",
                                    sources=["https://ai.google.dev/gemini-api/docs/models"]) is None  # 상시 문서는 무관


def test_publish_markers_carry_sources_and_the_record_accumulates():
    from autopost.history import history_from_posts, merge_published

    body = '<!--yoopost {"topic":"t","source":"https://a/1","category":"AI 도구","sources":["https://a/1","https://b/2"]}-->'
    posts = [{"url": "https://blog/p", "title": "글", "content": body, "published": "2026-09-30T10:00:00+09:00"}]
    assert history_from_posts(posts).entries[0].sources == ("https://a/1", "https://b/2")
    old = [HistoryEntry("2026-01-02", "옛 글", "옛 주제", "AI 도구", urls={"blogger": "https://blog/old"})]
    fresh = [HistoryEntry("2026-09-30", "새 글", "새 주제", "AI 도구", urls={"blogger": "https://blog/new"})]
    assert merge_published(old, fresh) == fresh  # 블로그 글을 전부 읽었으면 목록 그대로 (지운 글은 빠진다)
    assert [e.title for e in merge_published(old, fresh, limit=1)] == ["옛 글", "새 글"]  # 목록보다 오래된 글은 남긴다


def test_new_version_news_is_not_a_duplicate_even_on_the_same_changelog_page():
    log_url = "https://code.claude.com/docs/changelog"
    history = PostHistory((entry("Claude Code 2.1.280–2.1.285 변경점", url=log_url),
                           entry("Grok 4.7 출시 총정리 (xAI)", url="https://x.ai/news/grok-4-7")))
    assert history.find_duplicate("Claude Code 2.1.286–2.1.290 변경점", log_url) is None
    assert history.find_duplicate("Grok 5 출시 총정리 (xAI)", "https://x.ai/news/grok-5") is None
    found = history.find_duplicate("Claude Code 2.1.285 변경점 정리", log_url)
    assert found is not None and "같은 원문" in found[1]


def test_same_news_from_another_official_page_is_a_duplicate():
    anthropic = "https://www.anthropic.com/news/claude-sonnet-5-5"
    history = PostHistory((entry("Claude Sonnet 5.5 출시", "Introducing Claude Sonnet 5.5", anthropic),))
    found = history.find_duplicate("Claude Sonnet 5.5 now on Amazon Bedrock", "https://aws.amazon.com/blogs/sonnet-5-5",
                                   sources=["https://aws.amazon.com/blogs/sonnet-5-5", anthropic])
    assert found is not None and found[1] == "같은 소식의 원문을 함께 다룸"
    # 배경으로 인용했을 뿐 회사 이름 하나만 같은 다른 소식은 겹치지 않는다
    gpt = "https://openai.com/index/gpt-6-sol"
    other = PostHistory((entry("GPT-6 Sol 출시", "Announcing GPT-6 Sol and GPT-6 Luna", gpt),))
    assert other.find_duplicate("OpenAI DevDay 2026 발표 정리", "https://openai.com/devday",
                                sources=["https://openai.com/devday", gpt]) is None


def test_background_citations_of_the_same_product_family_are_not_duplicates():
    tts = "https://blog.google/gemini-models/gemini-3-8-text-to-speech/"
    rumor = entry("구글 Gemini 4 출시 소식 총정리: 아직 출시 전 예고 단계",
                  "구글 Gemini 4 출시 소식 — Google says Gemini 4 release is coming as soon as possible / "
                  "현재 Gemini 3.8 Flash 라인업", "https://9to5google.com/gemini-4",
                  sources=["https://9to5google.com/gemini-4", tts], where="최근 원고 r")
    found = PostHistory((rumor,)).find_duplicate(
        "Gemini 3.8 Flash TTS and Gemini 3.8 Flash-Lite TTS — Gemini 3.8 text-to-speech says hello", tts,
        title="Gemini 3.8 Flash TTS 사용 가이드: 음성 2,000개 이상·Voice Design 가격")
    assert found is None  # 루머 글이 배경으로 TTS 발표문을 인용했을 뿐 — 다른 소식


def test_pending_drafts_count_and_their_sources_are_compared():
    draft = entry("AMD의 World Labs 인수", "AMD to Acquire World Labs", "https://amd.com/news/world-labs",
                  sources=["https://amd.com/news/world-labs", "https://techcrunch.com/amd-world-labs"],
                  where="최근 원고 20261001_072950_4")
    history = combined(PostHistory(), [draft])
    found = history.find_duplicate("World Labs, AMD에 인수", "https://techcrunch.com/amd-world-labs",
                                   title="World Labs acquired by AMD")
    assert found is not None and found[0].where.startswith("최근 원고")


def test_similar_titles_still_duplicate_but_template_only_ipo_topics_do_not():
    history = PostHistory((entry("엘리스그룹 코스닥 공모 — 증권신고서(지분증권) 신주 2,180,000주"),))
    assert history.is_duplicate("엘리스그룹 코스닥 공모 — 증권신고서(지분증권) 신주 2,180,000주 정정")
    assert not history.is_duplicate("래블업 코스닥 공모 — 증권신고서(지분증권) 신주 2,180,000주")


def test_entries_keep_sources_through_the_snapshot_file():
    item = entry("t", url="https://a", sources=["https://a", "https://b"])
    assert HistoryEntry.from_dict(item.to_dict()).sources == ("https://a", "https://b")
    assert "sources" not in entry("t").to_dict()  # 블로그 글 기록은 그대로


def test_strict_history_load_refuses_to_continue_without_the_blog_list():
    class Broken:
        def recent_posts(self, limit):
            raise RuntimeError("503")

    assert load_history(Broken()).entries == ()
    with pytest.raises(HistoryUnavailable):
        load_history(Broken(), strict=True)
    with pytest.raises(HistoryUnavailable):
        load_history(None, strict=True)


# ------------------------------------------------------------------ 최신 이슈
def test_fresh_news_covered_by_two_sites_passes():
    sources = [src("https://openai.com/index/gpt-6-1", "2026-09-30", True),
               src("https://www.theverge.com/openai-gpt-6-1", "2026-09-30", sid="S2")]
    assert freshness_problems("AI 언어 모델", sources, "https://openai.com/index/gpt-6-1", TODAY) == []


def test_stale_primary_is_rejected_with_the_cutoff_date():
    sources = [src("https://api-docs.deepseek.com/news/v4-1", "2026-09-10", True),
               src("https://news.example.com/deepseek", "2026-09-30", sid="S2")]
    problems = freshness_problems("AI 언어 모델", sources, "https://api-docs.deepseek.com/news/v4-1", TODAY)
    assert len(problems) == 1 and "21일 전" in problems[0] and "2026-09-28 이후" in problems[0]
    alone = [src("https://api-docs.deepseek.com/news/v5", "2026-09-30", True)]
    assert "다른 매체" in freshness_problems("AI 언어 모델", alone, "https://api-docs.deepseek.com/news/v5", TODAY)[0]


def test_missing_dates_future_dates_and_missing_primary():
    assert "발표일" in freshness_problems("AI 도구", [src("https://a.com/x")], "https://a.com/x", TODAY)[0]
    assert "sources 에 없습니다" in freshness_problems("AI 도구", [src("https://a.com/x")], "https://b.com/y", TODAY)[0]
    future = [src("https://a.com/x", "2026-10-05"), src("https://b.com/y", "2026-10-01", sid="S2")]
    assert "뒤입니다" in freshness_problems("AI 도구", future, "https://a.com/x", TODAY)[0]


def test_one_company_on_several_subdomains_is_one_site_and_windows_differ_by_category():
    same_company = [src("https://blogs.nvidia.com/a", "2026-09-30", True),
                    src("https://nvidianews.nvidia.com/b", "2026-09-30", True, "S2")]
    assert issue_sites(same_company, "AI 뉴스 리뷰", TODAY) == set()  # 공식 발표끼리는 이슈화가 아니다
    # 같은 회사의 다른 도메인·보도자료 재배포처는 '다른 매체'가 아니다 (리뷰 fresh-2)
    echo = [src("https://www.anthropic.com/news/x", "2026-09-30", True), src("https://claude.com/blog/x", "2026-09-30"),
            src("https://www.businesswire.com/news/x", "2026-09-30"), src("https://www.financialcontent.com/x", "2026-09-30")]
    assert issue_sites(echo, "AI 뉴스 리뷰", TODAY, "https://www.anthropic.com/news/x") == set()
    press = echo + [src("https://techcrunch.com/x", "2026-09-30")]
    assert issue_sites(press, "AI 뉴스 리뷰", TODAY, "https://www.anthropic.com/news/x") == {"techcrunch.com"}
    paper = [src("https://arxiv.org/abs/2609.1", "2026-09-25", True), src("https://huggingface.co/papers/2609.1", "2026-09-26", sid="S2")]
    assert freshness_problems("AI 논문 분석", paper, "https://arxiv.org/abs/2609.1", TODAY) == []
    assert freshness_problems("AI 뉴스 리뷰", paper, "https://arxiv.org/abs/2609.1", TODAY)  # 뉴스는 3일
    assert window_days("AI 공모주 분석") == 14 and window_days("모르는 카테고리") == 3


def test_page_dates_catch_mislabelled_old_pages():
    assert page_published('<meta property="article:published_time" content="2026-09-10T08:00:00Z">') == date(2026, 9, 10)
    assert page_published('<meta content="2026/09/29" name="citation_date">') == date(2026, 9, 29)
    assert page_published('<script type="application/ld+json">{"datePublished": "2025-11-03"}</script>') == date(2025, 11, 3)
    assert page_published("<p>no date</p>") is None
    reject, note = page_date_check("2026-09-30", date(2025, 11, 3), "AI 도구", TODAY)
    assert "2025-11-03" in reject and note == ""
    reject, note = page_date_check("2026-09-30", date(2026, 9, 25), "AI 도구", TODAY)
    assert "2026-09-25" in reject  # 기간(9/28~) 밖이면 거절 — 시간대 차이 하루만 봐준다 (리뷰 fresh-3)
    reject, note = page_date_check("2026-10-01", date(2026, 9, 28), "AI 도구", TODAY)
    assert reject == "" and "2026-09-28" in note  # 둘 다 기간 안인데 다르면 참고로
    assert page_published('<p class="date">Published</p><meta name="published_time" content="Sep 23, 2026">') \
        == date(2026, 9, 23)
    assert page_published('{"datePublished": "23 September 2026"}') == date(2026, 9, 23)
    assert page_date_check("2026-09-30", None, "AI 도구", TODAY) == ("", "")


def test_folder_date_uses_the_draft_day():
    assert folder_date("20260930_235900_4", TODAY) == date(2026, 9, 30)
    assert folder_date("draft", TODAY) == TODAY
