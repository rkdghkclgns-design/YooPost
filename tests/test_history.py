from types import MappingProxyType

from autopost.constants import HISTORY_FILE
from autopost.history import HistoryEntry, HistoryStore, PostHistory
from fakes import MemoryDrive


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


def test_add_is_immutable_and_json_roundtrip():
    first = PostHistory()
    second = first.add(entry("A", urls={"naver": "https://n/a"}))
    assert first.entries == () and len(second.entries) == 1
    restored = PostHistory.from_json(second.to_json())
    assert restored.entries[0].urls["naver"] == "https://n/a"
    assert PostHistory.from_json("{broken").entries == ()
    assert PostHistory.from_json('{"posts": ["x", {"title": "T"}]}').entries[0].title == "T"


def test_store_creates_then_updates_file(tmp_path):
    drive = MemoryDrive()
    root, _ = drive.ensure_folder("root", "R")
    store = HistoryStore(drive, root.id, tmp_path)
    assert store.load().entries == ()
    store.save(PostHistory((entry("A"),)))
    store.save(store.load().add(entry("B")))
    files = [f for f in drive.children(root.id) if f.name == HISTORY_FILE]
    assert len(files) == 1
    assert [e.title for e in store.load().entries] == ["A", "B"]
