"""출처 주소 확인 — 검색 근거 주소 풀기, 실제로 열리는지·주제와 관련 있는지 점검.

- 구글 검색 근거(grounding) 주소는 vertexaisearch.cloud.google.com 경유 주소라 원래 주소로 푼다.
- 404·도메인 없음 등은 버린다 (지어낸 주소 차단).
- 봇 차단(401/403/429)은 '열리지만 확인 불가'로 표시 — 검색 근거로 실제 확인된 주소일 때만 쓴다.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import urlparse

log = logging.getLogger(__name__)

GROUNDING_HOSTS = ("vertexaisearch.cloud.google.com",)
BLOCKED_STATUSES = {401, 403, 405, 429, 503}
TIMEOUT = 12
MAX_BYTES = 400_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class LinkCheck:
    url: str  # 최종 주소 (리디렉션 반영)
    status: int  # 0 = 연결 실패
    title: str = ""
    mentions: bool = False  # 주제어가 페이지에 나오는지

    @property
    def alive(self) -> bool:
        return 200 <= self.status < 400

    @property
    def blocked(self) -> bool:
        return self.status in BLOCKED_STATUSES


def is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def canonical(url: str) -> str:
    """비교용 주소 (스킴·www·끝 슬래시·추적 파라미터 무시)."""
    parsed = urlparse(url)
    query = "&".join(p for p in parsed.query.split("&") if p and not p.startswith(("utm_", "ref=", "ref_src=")))
    return f"{domain(url)}{parsed.path.rstrip('/')}" + (f"?{query}" if query else "")


def _session(session):
    if session is not None:
        return session
    import requests

    created = requests.Session()
    created.headers["User-Agent"] = USER_AGENT
    created.headers["Accept-Language"] = "ko-KR,ko;q=0.9,en;q=0.8"
    return created


def resolve_grounding_url(url: str, session=None) -> str:
    """검색 근거 경유 주소 → 원래 주소 (실패하면 그대로)."""
    if (urlparse(url).hostname or "") not in GROUNDING_HOSTS:
        return url
    try:
        response = _session(session).get(url, allow_redirects=False, timeout=TIMEOUT)
        location = response.headers.get("Location") or response.headers.get("location") or ""
        return location if is_http_url(location) else url
    except Exception as exc:
        log.warning("검색 근거 주소 풀기 실패: %s", exc)
        return url


def _page_text(response) -> str:
    content_type = (response.headers.get("Content-Type") or response.headers.get("content-type") or "").lower()
    if "html" not in content_type and "text" not in content_type:
        return ""
    # 헤더에 charset 이 없으면 requests 는 ISO-8859-1 로 가정해 한글이 깨진다 → 내용으로 인코딩 추정
    encoding = response.encoding if "charset=" in content_type else getattr(response, "apparent_encoding", None)
    return response.content[:MAX_BYTES].decode(encoding or "utf-8", errors="ignore")


def _get(url: str, session, retries: int, wait: float):
    """연결 오류(시간 초과·끊김)는 잠깐 쉬고 다시 시도 — 일시적 장애로 멀쩡한 출처를 버리지 않도록."""
    for attempt in range(retries + 1):
        try:
            return session.get(url, timeout=TIMEOUT, allow_redirects=True)
        except Exception as exc:
            if attempt == retries:
                raise
            log.info("출처 열기 재시도 %s: %s", url, exc)
            time.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def check_link(url: str, keywords: Sequence[str] = (), session=None, retries: int = 1, wait: float = 2.0) -> LinkCheck:
    """주소를 열어 상태·제목·주제어 포함 여부를 확인한다 (계속 실패하면 status 0)."""
    if not is_http_url(url):
        return LinkCheck(url, 0)
    try:
        response = _get(url, _session(session), retries, wait)
    except Exception as exc:
        log.info("출처 열기 실패 %s: %s", url, exc)
        return LinkCheck(url, 0)
    final = str(getattr(response, "url", "") or url)
    text = _page_text(response) if 200 <= response.status_code < 400 else ""
    match = _TITLE.search(text)
    title = re.sub(r"\s+", " ", match.group(1)).strip() if match else ""
    lowered = _TAGS.sub(" ", text).lower()
    mentions = any(k.lower() in lowered for k in keywords if len(k) >= 2) if keywords else bool(text)
    return LinkCheck(final, response.status_code, title[:200], mentions)


def unique_urls(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for url in urls:
        key = canonical(url)
        if url and key not in seen:
            seen.add(key)
            result.append(url)
    return result
