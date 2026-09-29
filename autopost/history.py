"""발행 기록 — 같은 주제 반복 방지 · '함께 보면 좋은 글' 링크.

구글 블로그에 실제로 올라가 있는 글 목록이 곧 발행 기록이다 (별도 저장소·구글 드라이브를 쓰지 않는다).
글마다 본문 끝에 숨은 주석 <!--yoopost {"topic": 주제, "source": 원문 주소}--> 를 넣어 두고, 목록을 읽을 때 꺼낸다.
글을 블로그에서 지우면 기록에서도 빠진다.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from types import MappingProxyType
from typing import Mapping

from .constants import CATEGORIES
from .links import canonical

log = logging.getLogger(__name__)

SIMILAR_TITLE = 0.72  # 제목이 이만큼 비슷하면 같은 주제로 본다
MAX_ENTRIES = 1000
_NOISE = re.compile(r"[^0-9a-z가-힣]+")
_WORD = re.compile(r"[0-9a-z가-힣]+")
_NUMBER = re.compile(r"^\d+[가-힣]*$")  # 250만주 · 90500원 · 2026 같은 숫자(+단위)
_MARKER = re.compile(r"<!--yoopost (\{.*?\})-->", re.DOTALL)
# 카테고리 글의 주제·제목 틀에 늘 들어가는 말 — 이 말과 숫자만 겹치면 같은 주제로 보지 않는다
# (공모주 주제는 '<회사> 증권신고서(지분증권) — 코스닥 상장 공모 N주, 희망공모가 …' 틀이 같아 회사가 달라도 0.74 로 겹친다)
TEMPLATE_WORDS = frozenset({
    "증권신고서", "지분증권", "정정", "효력발생", "효력발생안내", "금융감독원", "한국거래소", "dart", "kind",
    "코스닥", "코스피", "코넥스", "유가증권시장", "상장", "공모", "공모주", "신주", "구주", "희망공모가", "희망가",
    "공모가", "밴드", "수요예측", "청약", "분석", "총정리", "사용", "가이드",
})


def _norm(text: str) -> str:
    return _NOISE.sub("", text.lower())


def _subject_words(text: str) -> frozenset[str]:
    """주제의 고유한 말(회사·모델 이름 등) — 틀 말·숫자·한 글자는 뺀다."""
    return frozenset(w for w in _WORD.findall(text.lower())
                     if len(w) >= 2 and w not in TEMPLATE_WORDS and not _NUMBER.match(w))


def _share_subject(a: frozenset[str], b: frozenset[str]) -> bool:
    """고유한 말이 하나라도 겹치는가 — 조사가 붙은 말('엘리스그룹의')도 같은 말로 본다. 한쪽이 비면 판단하지 않는다(True)."""
    if not a or not b:
        return True
    return any(x == y or x.startswith(y) or y.startswith(x) for x in a for y in b)


@dataclass(frozen=True)
class HistoryEntry:
    date: str  # YYYY-MM-DD
    title: str  # 발행한 글 제목
    topic: str  # 주제 (원 기사·발표 제목)
    category: str
    source_url: str = ""
    urls: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))  # 플랫폼 → 게시글 주소

    def to_dict(self) -> dict:
        return {"date": self.date, "title": self.title, "topic": self.topic, "category": self.category,
                "source_url": self.source_url, "urls": dict(self.urls)}

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        return cls(
            date=str(data.get("date", "")),
            title=str(data.get("title", "")),
            topic=str(data.get("topic", "")),
            category=str(data.get("category", "")),
            source_url=str(data.get("source_url", "")),
            urls=MappingProxyType({str(k): str(v) for k, v in (data.get("urls") or {}).items()}),
        )


@dataclass(frozen=True)
class PostHistory:
    entries: tuple[HistoryEntry, ...] = ()

    def is_duplicate(self, topic: str, url: str = "") -> bool:
        """같은 원문 주소이거나, 주제·제목이 거의 같은 글이 이미 있으면 True.

        글자 비율이 높아도 고유한 말(회사·모델 이름)이 하나도 겹치지 않으면 틀만 같은 다른 주제로 본다.
        """
        key = canonical(url) if url else ""
        wanted = _norm(topic)
        subject = _subject_words(topic)
        for entry in self.entries:
            if key and entry.source_url and canonical(entry.source_url) == key:
                return True
            for seen in (entry.topic, entry.title):
                norm = _norm(seen)
                if (wanted and norm and SequenceMatcher(None, wanted, norm).ratio() >= SIMILAR_TITLE
                        and _share_subject(subject, _subject_words(seen))):
                    return True
        return False

    def recent_titles(self, limit: int = 40) -> list[str]:
        return [e.title for e in self.entries[-limit:]][::-1]

    def related(self, platform: str, category: str, limit: int) -> list[tuple[str, str]]:
        """같은 카테고리 최근 글 우선, 모자라면 다른 최근 글 — (제목, 주소)."""
        published = [e for e in reversed(self.entries) if e.urls.get(platform)]
        same = [e for e in published if e.category == category]
        others = [e for e in published if e.category != category]
        return [(e.title, e.urls[platform]) for e in (same + others)[:limit]]


def read_marker(html: str) -> dict:
    """본문의 발행 표시 주석 → {"topic": ..., "source": ...} (없거나 깨졌으면 빈 dict)."""
    match = _MARKER.search(html or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def history_from_posts(posts) -> PostHistory:
    """Blogger 글 목록(최신순) → 발행 기록(오래된 순)."""
    entries = []
    for post in reversed(list(posts)):
        if not isinstance(post, dict) or not post.get("url"):
            continue
        marker = read_marker(str(post.get("content") or ""))
        labels = [str(label) for label in post.get("labels") or []]
        # Blogger 는 라벨 순서를 바꿔 돌려주므로 '첫 라벨'이 아니라 카테고리 목록에 있는 라벨을 쓴다
        category = str(marker.get("category") or next((label for label in labels if label in CATEGORIES), ""))
        title = str(post.get("title") or "")
        entries.append(HistoryEntry(
            date=str(post.get("published") or "")[:10],
            title=title,
            topic=str(marker.get("topic") or title),
            category=category,
            source_url=str(marker.get("source") or ""),
            urls=MappingProxyType({"blogger": str(post["url"])}),
        ))
    return PostHistory(tuple(entries[-MAX_ENTRIES:]))


def load_history(blogger, limit: int = 200) -> PostHistory:
    """구글 블로그 글 목록에서 발행 기록을 만든다 — 읽지 못하면 빈 기록으로 계속 (경고만)."""
    if blogger is None:
        return PostHistory()
    try:
        return history_from_posts(blogger.recent_posts(limit))
    except Exception as exc:
        log.warning("구글 블로그 글 목록을 읽지 못해 발행 기록 없이 진행합니다: %s", exc)
        return PostHistory()
