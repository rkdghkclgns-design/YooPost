"""AI 주제 후보 수집 — 공식 발표 RSS·AI 뉴스·논문·해커뉴스에서 최근 글을 모은다.

소스 하나가 실패해도 나머지로 계속한다. 외부 라이브러리 없이 표준 XML 파서로 RSS 2.0·Atom 을 읽는다.
"""
from __future__ import annotations

import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable, Sequence

from .constants import AI_KEYWORDS, Feed
from .links import canonical

log = logging.getLogger(__name__)

ATOM = "{http://www.w3.org/2005/Atom}"
KST = timezone(timedelta(hours=9))
PER_FEED = 12
SUMMARY_CHARS = 280
_TAGS = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[a-z0-9]+|[가-힣]+")


@dataclass(frozen=True)
class Candidate:
    title: str
    url: str
    source: str
    published: datetime | None
    summary: str = ""
    official: bool = False

    def label(self) -> str:
        when = self.published.astimezone(KST).strftime("%m/%d") if self.published else "날짜 미상"
        kind = "공식" if self.official else "뉴스"
        return f"[{kind}·{self.source}·{when}] {self.title}"


def parse_date(value: str | None) -> datetime | None:
    """RFC 822(RSS)·ISO 8601(Atom)·'YYYY-MM-DD HH:MM:SS'(시간대 없음 = KST) 날짜."""
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)


def clean_text(value: str | None, limit: int = SUMMARY_CHARS) -> str:
    text = html.unescape(_TAGS.sub(" ", value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def is_ai_related(text: str) -> bool:
    lowered = text.lower()
    words = set(_WORD.findall(lowered))
    return any((k in words) if k.isascii() and len(k) <= 4 else (k in lowered) for k in AI_KEYWORDS)


def parse_feed(data: bytes, feed: Feed) -> list[Candidate]:
    root = ET.fromstring(data)
    items: list[Candidate] = []
    for item in root.iter("item"):  # RSS 2.0
        items.append(Candidate(
            title=clean_text(item.findtext("title"), 200),
            url=(item.findtext("link") or "").strip(),
            source=feed.name,
            published=parse_date(item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date")),
            summary=clean_text(item.findtext("description")),
            official=feed.official,
        ))
    for entry in root.iter(f"{ATOM}entry"):  # Atom
        link = next((l.get("href", "") for l in entry.findall(f"{ATOM}link")
                     if l.get("rel", "alternate") == "alternate"), "")
        items.append(Candidate(
            title=clean_text(entry.findtext(f"{ATOM}title"), 200),
            url=link.strip(),
            source=feed.name,
            published=parse_date(entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")),
            summary=clean_text(entry.findtext(f"{ATOM}summary") or entry.findtext(f"{ATOM}content")),
            official=feed.official,
        ))
    return [c for c in items if c.title and c.url]


def parse_hf_papers(data: bytes, feed: Feed) -> list[Candidate]:
    items = []
    for row in json.loads(data):
        paper = row.get("paper") or {}
        paper_id = paper.get("id") or ""
        if not paper_id:
            continue
        items.append(Candidate(
            title=clean_text(paper.get("title") or row.get("title"), 200),
            url=f"https://huggingface.co/papers/{paper_id}",
            source=feed.name,
            published=parse_date(row.get("publishedAt") or paper.get("publishedAt")),
            summary=clean_text(paper.get("summary")),
            official=False,
        ))
    return items


def parse_hn(data: bytes, feed: Feed) -> list[Candidate]:
    items = []
    for hit in json.loads(data).get("hits", []):
        url = hit.get("url") or ""
        if not url:
            continue
        items.append(Candidate(
            title=clean_text(hit.get("title"), 200),
            url=url,
            source=feed.name,
            published=parse_date(hit.get("created_at")),
            summary=f"Hacker News {hit.get('points', 0)} points",
        ))
    return items


PARSERS = {"rss": parse_feed, "hf_papers": parse_hf_papers, "hn": parse_hn}


def fetch_feed(feed: Feed, session) -> list[Candidate]:
    response = session.get(feed.url, timeout=20)
    response.raise_for_status()
    return PARSERS[feed.kind](response.content, feed)


def dedupe(candidates: Iterable[Candidate]) -> list[Candidate]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    result = []
    for candidate in candidates:
        url_key, title_key = canonical(candidate.url), re.sub(r"\W", "", candidate.title.lower())
        if url_key in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        result.append(candidate)
    return result


def collect_candidates(
    feeds: Sequence[Feed], session, now: datetime, lookback_days: int, per_feed: int = PER_FEED
) -> list[Candidate]:
    """최근 lookback_days 일 안의 AI 관련 후보 — 공식 발표 먼저, 같은 등급은 최신순."""
    since = now - timedelta(days=lookback_days)
    collected: list[Candidate] = []
    for feed in feeds:
        try:
            items = fetch_feed(feed, session)
        except Exception as exc:  # 한 소스 실패가 전체를 멈추지 않도록
            log.warning("주제 소스 수집 실패 [%s]: %s", feed.name, exc)
            continue
        recent = [c for c in items if c.published is None or c.published >= since]
        if not feed.ai_only:
            recent = [c for c in recent if is_ai_related(f"{c.title} {c.summary}")]
        dated = sorted(recent, key=lambda c: c.published or since, reverse=True)
        collected.extend(dated[:per_feed])
    ordered = sorted(collected, key=lambda c: (not c.official, -(c.published or since).timestamp()))
    return dedupe(ordered)
