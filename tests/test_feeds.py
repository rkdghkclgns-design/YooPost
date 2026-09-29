import json
from datetime import datetime, timezone

from autopost.constants import Feed
from autopost.feeds import (
    Candidate,
    clean_text,
    collect_candidates,
    dedupe,
    is_ai_related,
    parse_date,
    parse_feed,
    parse_hf_papers,
    parse_hn,
)
from fakes import FakeHttp, FakeHttpResponse

NOW = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)
RSS = """<rss><channel>
<item><title>New Model &amp; API</title><link>https://openai.com/news/a</link>
<pubDate>Mon, 28 Sep 2026 07:00:00 GMT</pubDate><description><![CDATA[<p>Great model</p>]]></description></item>
<item><title>Old post</title><link>https://openai.com/news/old</link><pubDate>Mon, 01 Sep 2026 07:00:00 GMT</pubDate></item>
<item><title></title><link>https://openai.com/news/empty</link></item>
</channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>에이전트 신기능</title><link rel="alternate" href="https://news.hada.io/1"/>
<updated>2026-09-29T07:43:38+09:00</updated><summary>AI 에이전트 소식</summary></entry>
<entry><title>요리 레시피</title><link href="https://news.hada.io/2"/><updated>2026-09-29T07:00:00+09:00</updated></entry>
</feed>"""


def test_parse_rss_and_atom():
    rss = parse_feed(RSS.encode(), Feed("OpenAI", "u", official=True))
    assert [c.title for c in rss] == ["New Model & API", "Old post"]
    assert rss[0].summary == "Great model" and rss[0].official
    assert rss[0].published == datetime(2026, 9, 28, 7, tzinfo=timezone.utc)
    atom = parse_feed(ATOM.encode(), Feed("GeekNews", "u"))
    assert atom[0].url == "https://news.hada.io/1" and atom[1].url == "https://news.hada.io/2"


def test_parse_json_sources():
    papers = parse_hf_papers(json.dumps([
        {"paper": {"id": "2609.1", "title": "Big Paper", "summary": "abc"}, "publishedAt": "2026-09-28T00:00:00Z"},
        {"paper": {}},
    ]).encode(), Feed("HF", "u", kind="hf_papers"))
    assert papers[0].url == "https://huggingface.co/papers/2609.1"
    hn = parse_hn(json.dumps({"hits": [{"title": "AI thing", "url": "https://x.com", "points": 300,
                                         "created_at": "2026-09-28T10:00:00Z"}, {"title": "no url"}]}).encode(),
                  Feed("HN", "u", kind="hn"))
    assert len(hn) == 1 and "300 points" in hn[0].summary


def test_parse_date_formats():
    assert parse_date("2026-09-29 07:27:02").utcoffset().total_seconds() == 9 * 3600  # 시간대 없으면 KST
    assert parse_date("2026-09-29T00:00:00Z").tzinfo is not None
    assert parse_date("nonsense") is None and parse_date(None) is None


def test_ai_filter_uses_word_boundaries_for_short_keywords():
    assert is_ai_related("OpenAI releases GPT model")
    assert is_ai_related("새 AI 에이전트")
    assert not is_ai_related("Said the rain in Spain")  # 'ai' 가 단어 일부인 경우 제외
    assert clean_text("<b>a</b>\n  b", 10) == "a b"


def test_collect_filters_recent_ai_items_official_first_and_survives_failures():
    feeds = (
        Feed("GeekNews", "https://geek", ai_only=False),
        Feed("OpenAI", "https://openai", official=True),
        Feed("Broken", "https://broken"),
    )
    http = FakeHttp({
        "https://geek": FakeHttpResponse(200, ATOM),
        "https://openai": FakeHttpResponse(200, RSS),
        "https://broken": FakeHttpResponse(500),
    })
    items = collect_candidates(feeds, http, NOW, lookback_days=3)
    assert [c.title for c in items] == ["New Model & API", "에이전트 신기능"]
    assert items[0].label().startswith("[공식·OpenAI·09/28]")


def test_dedupe_by_url_and_title():
    a = Candidate("Same Title", "https://a.com/x", "A", None)
    b = Candidate("same title!", "https://b.com/y", "B", None)
    c = Candidate("Other", "https://www.a.com/x/", "C", None)
    assert dedupe([a, b, c]) == [a]
