"""오늘의 주제 선정.

1) 슬랙 등으로 주제를 직접 주면 그 주제 (웹 검색으로 대표 1차 자료를 찾음)
2) 아니면 공식 발표·AI 뉴스 후보 중 아직 안 쓴 것을 AI 가 골라준다
3) 후보가 하나도 없으면(소스 전부 실패) 웹 검색으로 최근 AI 발표를 찾는다
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Sequence

from .constants import CATEGORIES, CATEGORY_HINTS, DEFAULT_CATEGORY, FEEDS, Feed
from .content import ContentError, load_json_object
from .feeds import Candidate, collect_candidates
from .gemini import Gemini
from .history import PostHistory
from .links import LinkCheck, check_link, is_http_url

log = logging.getLogger(__name__)

MAX_CANDIDATES = 40


class TopicError(RuntimeError):
    """쓸 만한 주제를 찾지 못함."""


@dataclass(frozen=True)
class Topic:
    title: str  # 주제 (원 발표·기사 제목 또는 요청 문구)
    category: str
    angle: str  # 이 글에서 다룰 관점 (예: '스펙·가격·사용법 총정리')
    keywords: tuple[str, ...]
    source_url: str = ""
    source_name: str = ""
    official: bool = False
    requested: bool = False


def _category(value: object) -> str:
    text = str(value or "").strip()
    return text if text in CATEGORIES else DEFAULT_CATEGORY


def _keywords(value: object, fallback: str) -> tuple[str, ...]:
    items = [str(v).strip() for v in value] if isinstance(value, list) else []
    items = [v for v in items if v]
    return tuple(dict.fromkeys(items or [fallback]))[:6]


def _category_guide() -> str:
    return "\n".join(f"- {name}: {hint}" for name, hint in CATEGORY_HINTS.items())


def build_pick_prompt(candidates: Sequence[Candidate], recent_titles: Sequence[str], today: str) -> str:
    listing = "\n".join(
        f"{i}. {c.label()}\n   {c.url}\n   {c.summary[:200]}" for i, c in enumerate(candidates)
    )
    recent = "\n".join(f"- {t}" for t in recent_titles) or "- (없음)"
    return f"""당신은 한국어 AI 전문 블로그 'AI백과사전'의 편집장입니다. 오늘은 {today} 입니다.
아래 후보 중 오늘 쓸 글 주제 1개를 고르세요.

[고르는 기준]
- 한국 독자가 지금 궁금해할 만한 AI 소식 (새 모델·도구 출시, 가격·무료 정책, 사용법, 주목받는 논문, 큰 업계 소식)
- 공식 발표·1차 자료로 사실을 확인할 수 있는 주제 우선 ([공식] 표시가 1차 자료)
- 광고·행사 후기·채용·단순 인터뷰처럼 정보 가치가 낮은 글은 제외
- 최근에 쓴 글과 같은 주제는 제외

[최근에 쓴 글]
{recent}

[카테고리]
{_category_guide()}

[후보]
{listing}

[출력] JSON 객체 하나만:
{{"index": 후보 번호, "category": "위 카테고리 중 하나", "angle": "이 글에서 다룰 관점 한 줄",
  "keywords": ["핵심어(제품·모델·회사명) 2~5개"], "reason": "고른 이유 한 줄"}}"""


def build_request_prompt(requested: str, today: str) -> str:
    return f"""당신은 한국어 AI 전문 블로그 'AI백과사전'의 편집장입니다. 오늘은 {today} 입니다.
독자가 요청한 주제: "{requested}"
웹 검색으로 이 주제의 가장 최근·가장 신뢰할 수 있는 1차 자료(공식 발표·공식 문서·논문)를 찾아 글 주제를 정리하세요.
주제가 AI 와 관련 없거나 사실을 확인할 수 없으면 "ok": false 로 답하세요.

[카테고리]
{_category_guide()}

[출력] JSON 객체 하나만:
{{"ok": true, "title": "주제 (정확한 제품·모델·논문 이름 포함)", "category": "위 카테고리 중 하나",
  "angle": "이 글에서 다룰 관점 한 줄", "keywords": ["핵심어 2~5개"],
  "source_url": "대표 1차 자료 주소", "source_name": "발표 주체", "official": true}}"""


def build_search_prompt(recent_titles: Sequence[str], today: str, lookback_days: int) -> str:
    recent = "\n".join(f"- {t}" for t in recent_titles) or "- (없음)"
    return f"""오늘은 {today} 입니다. 웹 검색으로 최근 {lookback_days}일 안에 공식 발표된 AI 소식 중
한국어 AI 블로그 'AI백과사전'에 쓸 만한 주제 1개를 찾으세요 (새 모델·도구 출시, 가격 정책, 주목받는 논문 등).
최근에 쓴 글과 같은 주제는 제외합니다:
{recent}

[카테고리]
{_category_guide()}

[출력] JSON 객체 하나만:
{{"title": "주제", "category": "위 카테고리 중 하나", "angle": "관점 한 줄", "keywords": ["핵심어 2~5개"],
  "source_url": "공식 발표 주소", "source_name": "발표 주체", "official": true}}"""


class TopicSelector:
    def __init__(
        self,
        gemini: Gemini,
        session,
        feeds: Sequence[Feed] = FEEDS,
        lookback_days: int = 3,
        collect: Callable[..., list[Candidate]] = collect_candidates,
        checker: Callable[..., LinkCheck] = check_link,
    ) -> None:
        self._gemini = gemini
        self._session = session
        self._feeds = tuple(feeds)
        self._lookback_days = lookback_days
        self._collect = collect
        self._check = checker

    def _verified_url(self, url: str, keywords: Sequence[str]) -> str:
        """AI 가 알려준 원문 주소는 실제로 열리는지 확인 — 404·없는 주소면 비운다 (자료 조사가 출처를 다시 찾음)."""
        if not is_http_url(url):
            return ""
        check = self._check(url, list(keywords), self._session)
        if check.alive or check.blocked:
            return check.url if check.alive else url
        log.info("요청·검색 주제의 원문 주소를 확인하지 못해 비웁니다 (상태 %s): %s", check.status, url)
        return ""

    def pick(self, history: PostHistory, now: datetime, requested: str = "") -> tuple[Topic, int]:
        """(주제, 살펴본 후보 수)."""
        today = now.strftime("%Y-%m-%d")
        if requested.strip():
            return self._requested(requested.strip(), today), 0
        candidates = self._collect(self._feeds, self._session, now, self._lookback_days)
        fresh = [c for c in candidates if not history.is_duplicate(c.title, c.url)][:MAX_CANDIDATES]
        if fresh:
            return self._choose(fresh, history, today), len(candidates)
        log.warning("새 주제 후보가 없어 웹 검색으로 주제를 찾습니다 (수집 %d건)", len(candidates))
        return self._search(history, today), len(candidates)

    def _choose(self, fresh: Sequence[Candidate], history: PostHistory, today: str) -> Topic:
        def parse(text: str) -> dict:
            data = load_json_object(text)
            index = data.get("index")
            if not isinstance(index, int) or not 0 <= index < len(fresh):
                raise ContentError(f"후보 번호가 잘못되었습니다: {index!r}")
            return data

        data, _ = self._gemini.generate(
            build_pick_prompt(fresh, history.recent_titles(), today), parse, temperature=0.3, label="주제 선정"
        )
        chosen = fresh[data["index"]]
        return Topic(
            title=chosen.title,
            category=_category(data.get("category")),
            angle=str(data.get("angle") or "").strip(),
            keywords=_keywords(data.get("keywords"), chosen.title),
            source_url=chosen.url,
            source_name=chosen.source,
            official=chosen.official,
        )

    def _from_search(self, data: dict, requested: bool) -> Topic:
        title = str(data.get("title") or "").strip()
        if not title:
            raise ContentError("주제 제목이 비어 있습니다")
        keywords = _keywords(data.get("keywords"), title)
        return Topic(
            title=title,
            category=_category(data.get("category")),
            angle=str(data.get("angle") or "").strip(),
            keywords=keywords,
            source_url=self._verified_url(str(data.get("source_url") or "").strip(), keywords),
            source_name=str(data.get("source_name") or "").strip(),
            official=bool(data.get("official")),
            requested=requested,
        )

    def _requested(self, requested: str, today: str) -> Topic:
        data, _ = self._gemini.generate(
            build_request_prompt(requested, today), load_json_object, grounded=True, label="요청 주제 확인"
        )
        if data.get("ok") is False:
            raise TopicError(f"요청한 주제를 AI 관련 사실로 확인하지 못했습니다: {requested}")
        return self._from_search(data, requested=True)

    def _search(self, history: PostHistory, today: str) -> Topic:
        data, _ = self._gemini.generate(
            build_search_prompt(history.recent_titles(), today, self._lookback_days),
            load_json_object, grounded=True, label="주제 검색",
        )
        topic = self._from_search(data, requested=False)
        if history.is_duplicate(topic.title, topic.source_url):
            raise TopicError(f"새 주제를 찾지 못했습니다 (이미 쓴 주제: {topic.title})")
        return topic
