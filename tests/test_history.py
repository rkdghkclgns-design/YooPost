from types import MappingProxyType

from autopost.blog_html import marker_comment
from autopost.history import HistoryEntry, PostHistory, history_from_posts, load_history, read_marker
from fakes import FakeBlogger


def entry(title, category="AI 도구", url="", urls=None, topic=None):
    return HistoryEntry("2026-09-28", title, topic or title, category, url, MappingProxyType(urls or {}))


def test_duplicate_by_source_url_or_similar_title():
    history = PostHistory((entry("🤖 Gemini 3.8 Live 사용 가이드", url="https://deepmind.google/blog/x"),))
    assert history.is_duplicate("다른 제목", "https://www.deepmind.google/blog/x/")
    assert history.is_duplicate("Gemini 3.8 Live 사용 가이드!")
    assert not history.is_duplicate("Claude Sonnet 5.5 공개", "https://anthropic.com/news/x")


def test_related_prefers_same_category_and_platform_urls():
    history = PostHistory((
        entry("A", "AI 도구", urls={"blogger": "https://b/a"}),
        entry("B", "AI 언어 모델", urls={"blogger": "https://b/b", "naver": "https://n/b"}),
        entry("C", "AI 도구", urls={"naver": "https://n/c"}),
        entry("D", "AI 도구", urls={"blogger": "https://b/d"}),
    ))
    assert history.related("blogger", "AI 도구", 2) == [("D", "https://b/d"), ("A", "https://b/a")]
    assert history.related("blogger", "AI 도구", 5)[-1] == ("B", "https://b/b")
    assert history.recent_titles(2) == ["D", "C"]


def test_marker_roundtrip_and_broken_markers():
    marker = {"topic": "GPT-6 -- 공개", "source": "https://openai.com/news/gpt-6"}
    html = f"<p>본문</p>\n{marker_comment(marker)}"
    assert "-- " not in html.split("<!--yoopost ")[1][:-3]  # 주석 안에 '--' 가 들어가지 않게
    assert read_marker(html) == marker
    assert read_marker("<p>표시 없음</p>") == {}
    assert read_marker("<!--yoopost {broken}-->") == {}
    assert read_marker('<!--yoopost ["list"]-->') == {}


def test_history_from_blog_posts_is_oldest_first():
    posts = [  # Blogger 는 최신순
        {"title": "B", "url": "https://b/2", "published": "2026-09-29T10:00:00+09:00", "labels": ["AI 도구", "x"],
         "content": marker_comment({"topic": "원 기사 B", "source": "https://src/b"})},
        {"title": "A", "url": "https://b/1", "published": "2026-09-28T10:00:00+09:00", "labels": [], "content": ""},
        {"title": "no url"}, "garbage",
    ]
    history = history_from_posts(posts)
    assert [e.title for e in history.entries] == ["A", "B"]
    newest = history.entries[-1]
    assert (newest.date, newest.topic, newest.category, newest.source_url) == \
        ("2026-09-29", "원 기사 B", "AI 도구", "https://src/b")
    assert history.entries[0].topic == "A" and history.entries[0].urls == {"blogger": "https://b/1"}
    assert history.is_duplicate("다른 제목", "https://src/b/")


def test_load_history_survives_missing_or_failing_blog():
    assert load_history(None).entries == ()
    assert load_history(FakeBlogger(list_fail=True)).entries == ()
    blogger = FakeBlogger()
    blogger.publish("🤖 제목", marker_comment({"topic": "T", "source": "https://s"}), ["AI 도구"])
    assert load_history(blogger).entries[0].source_url == "https://s"
