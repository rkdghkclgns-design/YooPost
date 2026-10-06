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
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlparse

from .constants import CATEGORIES
from .links import canonical

log = logging.getLogger(__name__)

SIMILAR_TITLE = 0.72  # 제목이 이만큼 비슷하면 같은 주제로 본다
MAX_ENTRIES = 1000  # 발행 기록은 전부 본다 (최근 몇 편만 보면 오래전에 쓴 글과 겹친다)
SHARED_SUBJECT = 2  # 서로의 대표 원문을 출처로 쓴 두 글이 고유한 말을 이만큼 이상,
SHARED_RATIO = 0.4  # 그리고 짧은 쪽 고유한 말의 이 비율 이상 함께 쓰면 같은 소식 (제품군 이름만 같은 배경 인용은 아님)
SAME_NEWS_WORDS, SAME_NEWS_RATIO = 3, 0.6  # 인용이 겹치지 않아도 고유한 말이 이만큼 같으면 같은 소식
MAX_MARKER_SOURCES = 12  # 발행 표시 주석에 남기는 출처 주소 수
# 여러 글이 늘 함께 인용하는 상시 문서 — 함께 인용했다고 같은 소식이 아니다
_EVERGREEN = frozenset({"docs", "doc", "documentation", "pricing", "changelog", "release-notes", "releases", "models",
                        "api-reference", "reference", "help", "support", "faq", "about", "terms", "privacy", "status",
                        "wiki", "guide", "guides", "overview"})
_NOISE = re.compile(r"[^0-9a-z가-힣]+")
_WORD = re.compile(r"[0-9a-z가-힣]+")
_NUMBER = re.compile(r"^\d+[가-힣]*$")  # 250만주 · 90500원 · 2026 같은 숫자(+단위)
_MARKER = re.compile(r"<!--yoopost (\{.*?\})-->", re.DOTALL)
# 카테고리 글의 주제·제목 틀에 늘 들어가는 말 — 이 말과 숫자만 겹치면 같은 주제로 보지 않는다
# (공모주 주제는 '<회사> 증권신고서(지분증권) — 코스닥 상장 공모 N주, 희망공모가 …' 틀이 같아 회사가 달라도 0.74 로 겹친다)
TEMPLATE_WORDS = frozenset({
    "증권신고서", "지분증권", "정정", "효력발생", "효력발생안내", "금융감독원", "한국거래소", "dart", "kind",
    "코스닥", "코스피", "코넥스", "유가증권시장", "상장", "공모", "공모주", "신주", "구주", "희망공모가", "희망가",
    "공모가", "밴드", "수요예측", "청약", "분석", "총정리", "사용", "가이드", "확정", "공시", "증권발행실적보고서",
    "반기보고서", "분기보고서", "사업보고서", "투자설명서",
    # 영어 발표 제목에 흔한 말 — 회사 이름 하나만 같아도 같은 주제로 보지 않도록
    "the", "and", "for", "with", "from", "into", "your", "our", "new", "now", "how", "what", "why", "are", "its",
    "in", "to", "of", "on", "is", "at", "by", "as", "us", "an", "or", "vs", "via", "says", "say", "about", "more",
    "introducing", "announcing", "announces", "announced", "launches", "launch", "launched", "release", "released",
    "releases", "update", "updates", "available", "ai", "api", "model", "models", "blog", "news", "docs",
    # 카테고리마다 늘 나오는 말 — 이 말만 같다고 같은 소식이 아니다
    "agent", "agents", "agentic", "llm", "llms", "paper", "arxiv", "research", "report", "security", "attack",
    "에이전트", "논문", "리포트", "보고서", "설정", "출시", "공격", "소식", "발표", "정리", "보안", "취약점", "모델",
})
# 버전 번호 — 'GPT-6' 과 'GPT-6.1', 'Grok 4.7' 과 'Grok 5' 는 다른 소식이다.
# 원 발표 제목(topic)에서 제품 이름에 붙은 번호만 본다 — 날짜·수량·금액·퍼센트·점수·크기(17B)는 버전이 아니다.
_DATES = re.compile(r"\d{4}[-./]\d{1,2}(?:[-./]\d{1,2})?|(?:19|20)\d{2}\s*년?|\d{1,2}\s*[월일]|\d{1,2}/\d{1,2}(?:~\d{1,2})?")
_VERSION = re.compile(r"([a-z가-힣]+)([- ]?)v?(\d+(?:\.\d+)*)(?![\d,])([a-z가-힣%$]*)")
_VERSION_RANGE = re.compile(r"(\d+(?:\.\d+)+)\s*[–~\-]\s*v?(\d+(?:\.\d+)+)(?![\d.]*[a-z가-힣%])")
_NOT_PRODUCT = frozenset({  # 이 말 뒤의 번호는 버전이 아니다 (Top 10 · Day 2 · CVSS 9.8 · iOS 26 · Q2)
    "top", "day", "days", "part", "step", "steps", "vol", "no", "page", "pages", "week", "weeks", "q", "h", "cvss",
    "ios", "android", "windows", "macos", "ep", "episode", "season", "phase", "tier", "level", "rank", "pro", "plus",
    "max", "ultra", "chapter", "section", "table", "figure", "fig", "top-k", "k", "x", "series",
})


def _norm(text: str) -> str:
    return _NOISE.sub("", text.lower())


def _subject_words(text: str) -> frozenset[str]:
    """주제의 고유한 말(회사·모델 이름 등) — 틀 말·숫자·한 글자는 뺀다."""
    return frozenset(w for w in _WORD.findall(text.lower())
                     if len(w) >= 2 and w not in TEMPLATE_WORDS and not _NUMBER.match(w))


def versions(text: str) -> frozenset[str]:
    """원 발표 제목의 버전 번호 ('GPT-6.1 Sol' → {'6.1'}, 'Grok 5' → {'5'}, 'DeepSeek-V4.1' → {'4.1'}).

    제품 이름 바로 뒤(붙거나 - · 공백 하나)의 번호만 — 뒤에 단위(%·억·B·배…)가 붙거나 앞말이 Top·Day·CVSS 같은 말이면 뺀다.
    """
    cleaned = _DATES.sub(" ", text.lower())
    found = set()
    for word, _sep, number, tail in _VERSION.findall(cleaned):
        if tail or word in _NOT_PRODUCT or re.fullmatch(r"(?:19|20)\d{2}", number):
            continue
        if "." in number or len(number) <= 3:
            found.add(number)
    for start, end in _VERSION_RANGE.findall(cleaned):  # '2.1.280–2.1.285' 처럼 범위로 적은 버전의 끝
        if start in found:
            found.add(end)
    return frozenset(found)


def _other_version(a: str, b: str) -> bool:
    """두 원 발표 제목에 모두 버전 번호가 있는데 하나도 같지 않으면 다른 버전의 소식 (같은 변경 기록 주소라도 새 소식)."""
    mine, theirs = versions(a), versions(b)
    return bool(mine and theirs and not mine & theirs)


def _evergreen(url: str) -> bool:
    """사이트 첫 화면·문서·요금·변경 기록 같은 상시 페이지인가."""
    parts = [p for p in urlparse(url).path.lower().split("/") if p]
    return not parts or any(p in _EVERGREEN for p in parts)


def _same_word(x: str, y: str) -> bool:
    """같은 말 — 한글은 조사가 붙은 말('엘리스그룹의')도 같게, 영어는 글자까지 같을 때만 (code ≠ codex)."""
    if x == y:
        return True
    if x.isascii() or y.isascii():
        return False
    return x.startswith(y) or y.startswith(x)


def _shared_count(a: frozenset[str], b: frozenset[str]) -> int:
    """함께 쓰는 고유한 말의 수 — 한 말이 여러 말과 맞아도 한 번만 센다 (양쪽에서 센 값 중 작은 쪽)."""
    left = sum(1 for x in a if any(_same_word(x, y) for y in b))
    right = sum(1 for y in b if any(_same_word(x, y) for x in a))
    return min(left, right)


def _same_subject(a: frozenset[str], b: frozenset[str], least: int = SHARED_SUBJECT,
                  ratio: float = SHARED_RATIO) -> bool:
    """고유한 말이 least 개 이상이면서 짧은 쪽의 ratio 이상 겹친다."""
    shared = _shared_count(a, b)
    return shared >= least and shared >= ratio * min(len(a), len(b))


def _share_subject(a: frozenset[str], b: frozenset[str]) -> bool:
    """고유한 말이 하나라도 겹치는가 — 한쪽이 비면 판단하지 않는다(True)."""
    if not a or not b:
        return True
    return _shared_count(a, b) >= 1


@dataclass(frozen=True)
class HistoryEntry:
    date: str  # YYYY-MM-DD
    title: str  # 발행한 글 제목
    topic: str  # 주제 (원 기사·발표 제목)
    category: str
    source_url: str = ""
    urls: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))  # 플랫폼 → 게시글 주소
    sources: tuple[str, ...] = ()  # 출처 주소 전부 (원고 브랜치의 원고만 — 블로그 글 기록에는 대표 원문만 있다)
    where: str = ""  # 알림용 — '발행한 글' · '최근 원고 <폴더>'
    draft_id: str = ""  # 발행 응답·상태 저장이 끊겨도 같은 원고를 찾아 복구한다

    def to_dict(self) -> dict:
        data = {"date": self.date, "title": self.title, "topic": self.topic, "category": self.category,
                "source_url": self.source_url, "urls": dict(self.urls)}
        if self.sources:
            data["sources"] = list(self.sources)
        if self.draft_id:
            data["draft_id"] = self.draft_id
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        return cls(
            date=str(data.get("date", "")),
            title=str(data.get("title", "")),
            topic=str(data.get("topic", "")),
            category=str(data.get("category", "")),
            source_url=str(data.get("source_url", "")),
            urls=MappingProxyType({str(k): str(v) for k, v in (data.get("urls") or {}).items()}),
            sources=tuple(str(u) for u in data.get("sources") or () if u),
            draft_id=str(data.get("draft_id") or ""),
        )


@dataclass(frozen=True)
class PostHistory:
    entries: tuple[HistoryEntry, ...] = ()

    def is_duplicate(self, topic: str, url: str = "") -> bool:
        return self.find_duplicate(topic, url) is not None

    def find_duplicate(self, topic: str, url: str = "", title: str = "",
                       sources: Iterable[str] = ()) -> tuple[HistoryEntry, str] | None:
        """이미 있는 글 가운데 이 글과 겹치는 것 — (그 글, 이유) 또는 None.

        - 같은 원문 주소면 같은 소식.
        - 한쪽이 다른 쪽의 대표 원문을 출처로 쓰거나 같은 기사(상시 문서 제외)를 함께 인용하고, 고유한 말(회사·제품 이름)이
          2개 이상·짧은 쪽의 40% 이상 같으면 같은 소식 (제품군 이름만 같은 배경 인용은 아님).
        - 고유한 말이 3개 이상·짧은 쪽의 60% 이상 같으면 같은 소식 (다른 매체 기사로 다시 쓴 것 — 어순·표현만 다름).
        - 주제·제목 글자가 거의 같고 고유한 말이 하나라도 겹치면 같은 주제 (틀만 같은 공모주 주제는 다른 주제).
        - 두 원 발표 제목(topic)에 모두 버전 번호가 있는데 모두 다르면('GPT-6' ↔ 'GPT-6.1') 새 소식 — 겹치지 않는다.
        """
        key = canonical(url) if url else ""
        cited = {canonical(u) for u in sources if u} | ({key} if key else set())
        news = {u for u in cited if not _evergreen(u)}
        texts = [t for t in (topic, title) if t]
        subject = _subject_words(" ".join(texts))
        for entry in self.entries:
            if _other_version(topic or title, entry.topic or entry.title):
                continue
            theirs = _subject_words(" ".join(t for t in (entry.topic, entry.title) if t))
            their_key = canonical(entry.source_url) if entry.source_url else ""
            if key and their_key == key:
                return entry, f"같은 원문 {entry.source_url}"
            their_cited = {canonical(u) for u in entry.sources if u} | ({their_key} if their_key else set())
            linked = ((key and key in their_cited and not _evergreen(key))
                      or (their_key and their_key in cited and not _evergreen(their_key)) or bool(news & their_cited))
            if linked and _same_subject(subject, theirs):
                return entry, "같은 소식의 원문을 함께 다룸"
            if _same_subject(subject, theirs, SAME_NEWS_WORDS, SAME_NEWS_RATIO):
                return entry, "주제 낱말이 거의 같음 (같은 소식)"
            for text in texts:
                wanted, words = _norm(text), _subject_words(text)
                for seen in (entry.topic, entry.title):
                    norm = _norm(seen)
                    if (wanted and norm and SequenceMatcher(None, wanted, norm).ratio() >= SIMILAR_TITLE
                            and _share_subject(words, _subject_words(seen))):
                        return entry, "주제·제목이 거의 같음"
        return None

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
            sources=tuple(str(u) for u in marker.get("sources") or () if isinstance(u, str) and u)[:MAX_MARKER_SOURCES],
            draft_id=str(marker.get("draft_id") or ""),
        ))
    return PostHistory(tuple(entries[-MAX_ENTRIES:]))


def merge_published(old: Sequence[HistoryEntry], fresh: Sequence[HistoryEntry],
                    limit: int = MAX_ENTRIES) -> list[HistoryEntry]:
    """발행 기록 파일을 누적한다 — 블로그 목록(최근 limit 편)이 덮는 기간은 목록 그대로(지운 글은 빠짐),
    그보다 오래된 글은 예전 기록을 남긴다 (글이 limit 편을 넘어도 오래전 글과 겹치지 않도록)."""
    fresh = list(fresh)
    if len(fresh) < limit:
        return fresh  # 블로그 글 전부를 읽었다
    oldest = min((e.date for e in fresh if e.date), default="")
    seen = {e.urls.get("blogger") for e in fresh}
    kept = [e for e in old if e.date and e.date < oldest and e.urls.get("blogger") not in seen]
    return kept + fresh


class HistoryUnavailable(RuntimeError):
    """구글 블로그 글 목록을 읽지 못함 — 빈 기록으로 계속하면 중복 검사가 꺼진다 (다음 실행에 다시)."""


def load_history(blogger, limit: int = MAX_ENTRIES, strict: bool = False) -> PostHistory:
    """구글 블로그 글 목록(전부)에서 발행 기록을 만든다.

    strict 이면 읽지 못했을 때 HistoryUnavailable — 중복 검사·발행 기록 파일은 빈 기록으로 진행하면 안 된다.
    아니면 경고만 남기고 빈 기록 (작성 지침처럼 보여 주기만 하는 곳).
    """
    if blogger is None:
        if strict:
            raise HistoryUnavailable("구글 블로그에 연결되어 있지 않습니다")
        return PostHistory()
    try:
        return history_from_posts(blogger.recent_posts(limit))
    except Exception as exc:
        if strict:
            raise HistoryUnavailable(f"구글 블로그 글 목록을 읽지 못했습니다: {exc}") from exc
        log.warning("구글 블로그 글 목록을 읽지 못해 발행 기록 없이 진행합니다: %s", exc)
        return PostHistory()


def combined(history: PostHistory, others: Sequence[HistoryEntry]) -> PostHistory:
    """발행한 글 + 아직 발행 전인 원고 (✅ 전이라 블로그에 없어도 같은 소식을 또 쓰지 않도록)."""
    return PostHistory(tuple(history.entries) + tuple(others))
