"""최신 이슈만 쓴다 (2026-10-01 사용자 지시) — 대표 원문 발표일·이슈화(다른 매체가 다룸)를 기계적으로 확인한다.

- 대표 원문(research.json source_url)의 발표일(published)이 원고 작성일(KST)로부터 카테고리별 날수 안이어야 한다.
- 그 기간 안에 이 소식을 다룬 **다른 매체·커뮤니티**(공식 출처와 다른 회사, 보도자료 재배포처 제외)가 1곳 이상이어야 한다.
- 사용자가 슬랙에서 주제를 정해 지시한 원고(수동 지시 single + 주제)는 날짜를 따지지 않는다 — 중복 검사는 그대로.
기간은 넓히지 않는다: 기간 안의 소식이 없으면 건너뛴다 (예전 '기간 넓히기'는 최신 이슈 지시로 폐지).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Sequence

from .links import canonical
from .quote_images import site
from .research import Source

FRESH_DAYS = MappingProxyType({  # 대표 원문 발표일이 원고 작성일로부터 며칠 안이어야 하는가
    "AI 언어 모델": 3,
    "AI 도구": 3,
    "AI 뉴스 리뷰": 3,
    "AI 논문 분석": 7,
    "AI 보안": 7,
    "AI 인사이트": 7,
    "AI 공모주 분석": 14,
})
DEFAULT_DAYS = 3
INDEPENDENT_SITES = 1  # 기간 안에 이 소식을 다룬 다른 매체·커뮤니티 수 (공식 출처와 다른 회사)
PAGE_SLACK_DAYS = 1  # 원문 페이지에 적힌 발표일이 기간보다 이만큼 넘게 오래되면 거절 (시간대 차이만 봐준다)
DATE_MISMATCH_DAYS = 2  # 원고 표기와 페이지 발표일이 이보다 다르면 승인 요청에 '참고'로 표시

# 같은 회사의 서로 다른 도메인 — 한 회사로 센다
COMPANY_SITES = MappingProxyType({
    "claude.com": "anthropic", "claude.ai": "anthropic", "anthropic.com": "anthropic",
    "openai.com": "openai", "chatgpt.com": "openai",
    "google": "google", "blog.google": "google", "deepmind.google": "google", "google.com": "google",
    "googleblog.com": "google", "withgoogle.com": "google", "googleapis.com": "google",
    "github.com": "github", "github.blog": "github", "githubnext.com": "github",
    "microsoft.com": "microsoft", "azure.com": "microsoft", "bing.com": "microsoft",
    "meta.com": "meta", "fb.com": "meta", "facebook.com": "meta",
    "x.ai": "xai", "nvidia.com": "nvidia", "amd.com": "amd", "apple.com": "apple",
    "dart.fss.or.kr": "dart", "fss.or.kr": "dart", "kind.krx.co.kr": "krx", "krx.co.kr": "krx",
})
# 보도자료 재배포처 — 회사가 낸 글을 그대로 싣는 곳이라 '다른 매체가 다룸'으로 치지 않는다
SYNDICATION_SITES = frozenset({
    "businesswire.com", "prnewswire.com", "globenewswire.com", "financialcontent.com", "newswire.com",
    "accesswire.com", "einpresswire.com", "newswire.co.kr", "prweb.com", "marketscreener.com", "stocktitan.net",
    "alphaxiv.org", "arxiv.org", "paperswithcode.com", "semanticscholar.org",
})

_DAY = re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_WORD_DAY = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", re.I)
_DAY_WORD = re.compile(r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+(\d{4})", re.I)
_DATE_NAMES = (r"(?:article:published_time|og:published_time|article\.published|article:published|published_time|"
               r"datePublished|pubdate|publishdate|publish_date|publish-date|date|citation_publication_date|"
               r"citation_online_date|citation_date|dc\.date|dc\.date\.issued|dcterms\.created|dcterms\.issued|"
               r"parsely-pub-date|sailthru\.date)")
_META_DATE = re.compile(
    r"<meta[^>]+(?:property|name|itemprop)=[\"']" + _DATE_NAMES + r"[\"'][^>]*content=[\"']([^\"']+)", re.I)
_META_DATE_REVERSED = re.compile(
    r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name|itemprop)=[\"']" + _DATE_NAMES + r"[\"']", re.I)
_JSONLD_DATE = re.compile(r"[\"']datePublished[\"']\s*:\s*[\"']([^\"']+)", re.I)


def window_days(category: str) -> int:
    return FRESH_DAYS.get(category, DEFAULT_DAYS)


def parse_day(value: object) -> date | None:
    """'2026-09-29' · '2026/09/29' · '2026.09.29' · ISO 시각 · 'Sep 23, 2026' · '23 September 2026' → 날짜."""
    text = str(value or "")
    try:
        match = _DAY.search(text)
        if match:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        match = _WORD_DAY.search(text)
        if match:
            return date(int(match.group(3)), _MONTHS[match.group(1).lower()[:3]], int(match.group(2)))
        match = _DAY_WORD.search(text)
        if match:
            return date(int(match.group(3)), _MONTHS[match.group(2).lower()[:3]], int(match.group(1)))
    except ValueError:
        return None
    return None


def folder_date(name: str, fallback: date) -> date:
    """원고 폴더 이름(YYYYMMDD_HHMMSS_n)의 날짜 — 기간은 원고를 쓴 날 기준 (Actions 가 다음 날 보내도 같게)."""
    try:
        return datetime.strptime(name[:8], "%Y%m%d").date()
    except ValueError:
        return fallback


def cutoff(category: str, day: date) -> date:
    return day - timedelta(days=window_days(category))


def company(url: str) -> str:
    """출처 주소의 회사 (같은 회사의 여러 도메인은 하나로)."""
    name = site(url)
    return COMPANY_SITES.get(name, name)


def primary_source(sources: Sequence[Source], source_url: str) -> Source | None:
    key = canonical(source_url) if source_url else ""
    return next((s for s in sources if key and canonical(s.url) == key), None)


def _in_window(source: Source, oldest: date, day: date) -> bool:
    published = parse_day(source.published)
    return published is not None and oldest <= published <= day + timedelta(days=1)


def issue_sites(sources: Sequence[Source], category: str, day: date, source_url: str = "") -> set[str]:
    """기간 안에 이 소식을 다룬 다른 매체·커뮤니티 (공식 출처·대표 원문과 다른 회사, 보도자료 재배포처 제외)."""
    oldest = cutoff(category, day)
    owners = {company(s.url) for s in sources if s.official}
    if source_url:
        owners.add(company(source_url))
    return {company(s.url) for s in sources
            if not s.official and _in_window(s, oldest, day) and company(s.url) not in owners
            and site(s.url) not in SYNDICATION_SITES}


def freshness_problems(category: str, sources: Sequence[Source], source_url: str, day: date) -> list[str]:
    """최신 이슈 조건을 못 맞춘 이유들 (맞으면 빈 목록)."""
    days, oldest = window_days(category), cutoff(category, day)
    primary = primary_source(sources, source_url)
    if primary is None:
        return ["대표 원문(research.json source_url)이 sources 에 없습니다 — 최신 여부를 확인할 수 없습니다"]
    published = parse_day(primary.published)
    if published is None:
        return [f"대표 원문의 발표일(sources 의 published, YYYY-MM-DD)이 없습니다 — 최신 이슈만 씁니다 "
                f"({category}: {oldest:%Y-%m-%d} 이후 발표). 날짜가 없는 문서면 날짜가 적힌 공식 발표(공지·블로그·보도자료)를 "
                "source_url 로 바꾸세요"]
    problems = []
    if published > day + timedelta(days=1):
        problems.append(f"대표 원문 발표일 {published:%Y-%m-%d} 이 원고 작성일({day:%Y-%m-%d})보다 뒤입니다 — 날짜를 확인하세요")
    elif published < oldest:
        problems.append(f"대표 원문 발표일 {published:%Y-%m-%d} 은 {(day - published).days}일 전입니다 — "
                        f"{category} 는 최근 {days}일({oldest:%Y-%m-%d} 이후) 발표된 소식만 씁니다")
    covered = issue_sites(sources, category, day, source_url)
    if len(covered) < INDEPENDENT_SITES:
        problems.append(f"최근 {days}일 안에 이 소식을 다룬 다른 매체·커뮤니티 출처가 없습니다 — 이슈가 되고 있는 소식만: "
                        "공식 발표 외에 다른 회사의 매체·커뮤니티(official: false, 보도자료 재배포처 제외) 1곳 이상을 "
                        "직접 읽고 출처로 넣으세요 (페이지에 적힌 published)")
    return problems


def page_published(page_html: str) -> date | None:
    """원문 페이지가 스스로 밝힌 발표일 (메타 태그·JSON-LD) — 없으면 None."""
    for pattern in (_META_DATE, _META_DATE_REVERSED, _JSONLD_DATE):
        for match in pattern.finditer(page_html or ""):
            found = parse_day(match.group(1))
            if found is not None:
                return found
    return None


def page_date_check(claimed: str, page_day: date | None, category: str, day: date) -> tuple[str, str]:
    """(거절 이유, 참고 문구) — 원문 페이지 발표일이 기간 밖이면 거절, 원고 표기와 다르면 참고."""
    if page_day is None:
        return "", ""
    oldest = cutoff(category, day)
    if page_day < oldest - timedelta(days=PAGE_SLACK_DAYS):
        return (f"대표 원문 페이지에 적힌 발표일이 {page_day:%Y-%m-%d} 입니다 — 최근 {window_days(category)}일 안의 "
                f"소식이 아닙니다 (원고 표기 {claimed or '없음'})"), ""
    written = parse_day(claimed)
    if written is not None and abs((written - page_day).days) > DATE_MISMATCH_DAYS:
        return "", f"발표일 확인 필요: 원고 {written:%Y-%m-%d} · 원문 페이지 {page_day:%Y-%m-%d}"
    return "", ""


def freshness_lines(category: str, day: date) -> list[str]:
    """작성 지침에 보여 줄 최신 이슈 조건."""
    days, oldest = window_days(category), cutoff(category, day)
    return [
        f"[최신 이슈만 — {category}] 오늘 {day:%Y-%m-%d} (KST). 대표 원문 발표일이 {oldest:%Y-%m-%d} 이후(최근 {days}일)인 소식만.",
        "  - 이슈화: 그 기간에 이 소식을 다룬 다른 회사의 매체·커뮤니티(official: false — 보도자료 재배포처 제외) 1곳 이상을 "
        "직접 읽고 출처로 넣는다. 모든 출처에 페이지에 적힌 published(YYYY-MM-DD).",
        "  - 기간을 넓히지 않는다. 기간 안의 소식이 모두 이미 쓴 글과 겹치면 건너뛴다 (check 가 오래된 소식을 거절한다).",
        "  - 여러 소식 중에서는 여러 매체가 다루는 가장 큰 이슈를 고른다. 사용법·총정리 같은 상시 주제는 매일 원고로 쓰지 않는다.",
        "  - 발표일·출처·카테고리를 바꿔 check 를 통과시키지 않는다 — 거절되면 기간 안의 다른 소식으로 바꾼다.",
    ]
