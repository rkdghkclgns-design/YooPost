"""인용 이미지 확보 — 자료 조사(구글 검색)가 찾은 이미지 후보를 실제로 내려받아 확인한다.

통과 기준 (하나라도 어기면 버린다 — 포스팅에 이미지가 필요 없으면 후보 자체가 없다):
  - 이미지가 실린 페이지가 조사에서 확인된 출처와 같은 사이트 (blogs.nvidia.com ↔ nvidianews.nvidia.com 은 같은 곳으로 봄)
  - 실제로 내려받아지는 이미지 파일 (주소를 지어냈으면 404 → 탈락). 안 되면 그 페이지의 대표 이미지(og:image)
  - 너무 작지 않음 (아이콘·로고 제외), 극단적으로 길쭉하지 않음
  - 같은 이미지 중복 제거
내려받은 이미지는 본문 폭에 맞게 줄이고 JPEG(투명 배경이면 PNG)로 저장한다.
"""
from __future__ import annotations

import hashlib
import html
import io
import logging
import re
from pathlib import Path
from typing import Sequence
from urllib.parse import urljoin

from PIL import Image

from .links import TIMEOUT, domain, is_http_url
from .research import ImageCandidate, QuoteImage, Source

log = logging.getLogger(__name__)

MAX_IMAGES = 3
MAX_DOWNLOAD = 8 * 1024 * 1024
MAX_PAGE = 400_000
MIN_WIDTH = 480
MIN_HEIGHT = 200
MAX_RATIO = 4.0
MAX_WIDTH = 1400
MAX_CAPTION = 120
_OG_IMAGE = (
    re.compile(r'<meta[^>]+(?:property|name)=["\']og:image(?::url)?["\'][^>]*content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:image(?::url)?["\']', re.I),
)


def parse_candidates(raw) -> tuple[ImageCandidate, ...]:
    """자료 조사 JSON 의 images 배열 → 후보 목록 (주소가 이상한 항목은 버림, 최대 3개)."""
    result: list[ImageCandidate] = []
    for index, item in enumerate(raw if isinstance(raw, list) else [], start=1):
        if not isinstance(item, dict):
            continue
        image_url = str(item.get("image_url") or item.get("url") or "").strip()
        page_url = str(item.get("page_url") or item.get("source_url") or "").strip()
        caption = " ".join(str(item.get("caption") or "").split())[:MAX_CAPTION]
        if not (is_http_url(image_url) or is_http_url(page_url)) or not caption:
            continue
        result.append(ImageCandidate(
            id=str(item.get("id") or f"I{index}").strip().upper() or f"I{index}",
            image_url=image_url if is_http_url(image_url) else "",
            page_url=page_url if is_http_url(page_url) else "",
            caption=caption,
            credit=" ".join(str(item.get("credit") or "").split())[:60],
        ))
    return tuple(result[:MAX_IMAGES])


_SECOND_LEVEL = {"co", "or", "go", "ac", "ne", "re", "com", "org", "net", "gov", "edu"}
# 누구나 하위 주소를 만들 수 있는 공유 호스팅 — 하위 주소까지 같아야 같은 사이트로 본다
SHARED_HOSTS = (
    "github.io", "gitlab.io", "blogspot.com", "wordpress.com", "tistory.com", "medium.com", "substack.com",
    "notion.site", "vercel.app", "netlify.app", "pages.dev", "workers.dev", "web.app", "firebaseapp.com",
    "herokuapp.com", "azurewebsites.net", "cloudfront.net", "amazonaws.com", "googleusercontent.com",
    "huggingface.co", "hf.space",
)


def site(url: str) -> str:
    """등록 도메인 (blogs.nvidia.com → nvidia.com, news.naver.co.kr → naver.co.kr, a.github.io → a.github.io)."""
    host = domain(url)
    if any(host == shared or host.endswith("." + shared) for shared in SHARED_HOSTS):
        return host
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _has_alpha(image: Image.Image) -> bool:
    """실제로 투명한 부분이 있는지 (투명 배경 도표는 PNG 로 둬야 깨지지 않는다)."""
    if image.mode == "P":
        return "transparency" in image.info
    if image.mode in ("RGBA", "LA"):
        return image.getchannel("A").getextrema()[0] < 255
    return False


def _read_capped(response, limit: int, truncate: bool = False) -> bytes | None:
    """응답 본문을 limit 바이트까지만 읽는다 — 큰 파일을 통째로 메모리에 올리지 않도록.

    truncate=False 면 limit 을 넘는 본문은 버린다(None), True 면 앞부분만 쓴다 (HTML 대표 이미지 찾기용).
    """
    declared = str(response.headers.get("Content-Length") or response.headers.get("content-length") or "")
    if not truncate and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in response.iter_content(64 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                if not truncate:
                    return None
                break
    finally:
        close = getattr(response, "close", None)
        if close is not None:
            close()
    return b"".join(chunks)[:limit]


AUTO_IMAGES = 2  # 원고에 쓸 이미지가 없을 때 공식 출처에서 보충하는 최대 개수
AUTO_PAGES = 4  # 대표 이미지를 찾아볼 공식 출처 페이지 수 (같은 그림은 내려받을 때 걸러진다)


def auto_candidates(sources: Sequence[Source]) -> tuple[ImageCandidate, ...]:
    """원고에 쓸 이미지가 하나도 없을 때 — 공식 출처 페이지의 대표 이미지(og:image) 후보.

    작성자가 이미지를 빠뜨려도(클라우드 원고는 자주 그랬다) 본문에 그림이 들어가게 하는 보충 장치.
    공식 출처(official)만 쓴다 — 회사가 공유용으로 내건 대표 이미지이고, 출처·저작권자를 캡션에 밝힌다.
    """
    result: list[ImageCandidate] = []
    pages: set[str] = set()
    for source in sources:
        if not source.official or not is_http_url(source.url) or source.url in pages:
            continue
        pages.add(source.url)
        name = site(source.url)
        result.append(ImageCandidate(
            id=f"A{len(result) + 1}", image_url="", page_url=source.url,
            caption=(" ".join((source.title or name).split()))[:MAX_CAPTION],
            credit=(" ".join((source.publisher or name).split()))[:60],
        ))
        if len(result) >= AUTO_PAGES:
            break
    return tuple(result)


def og_image(page_html: str, page_url: str) -> str:
    for pattern in _OG_IMAGE:
        match = pattern.search(page_html)
        if match:
            url = urljoin(page_url, html.unescape(match.group(1).strip()))
            return url if is_http_url(url) else ""
    return ""


class ImageCollector:
    def __init__(self, session=None) -> None:
        if session is None:
            import requests

            session = requests.Session()
        self._session = session

    def collect(self, candidates: Sequence[ImageCandidate], sources: Sequence[Source], workdir: Path
                ) -> tuple[QuoteImage, ...]:
        allowed = {site(s.url) for s in sources}
        publishers = {site(s.url): s.publisher for s in sources}
        kept: list[QuoteImage] = []
        hashes: set[str] = set()
        for candidate in candidates:
            page_site = site(candidate.page_url or candidate.image_url)
            if page_site not in allowed:
                log.info("인용 이미지 제외 (확인된 출처 사이트가 아님): %s", candidate.page_url or candidate.image_url)
                continue
            data = self._download(candidate.image_url) or self._download(self._page_og_image(candidate.page_url))
            if data is None:
                log.info("인용 이미지 제외 (내려받기 실패): %s", candidate.image_url or candidate.page_url)
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in hashes:
                continue
            saved = self._save(data, workdir / f"quote_{candidate.id}")
            if saved is None:
                continue
            hashes.add(digest)
            path, width, height = saved
            credit = candidate.credit or publishers.get(page_site) or page_site
            kept.append(QuoteImage(candidate.id, candidate.caption, credit,
                                   candidate.page_url or candidate.image_url, path, width, height))
        return tuple(kept)

    def _page_og_image(self, page_url: str) -> str:
        if not page_url:
            return ""
        try:
            response = self._session.get(page_url, timeout=TIMEOUT, allow_redirects=True, stream=True)
            body = _read_capped(response, MAX_PAGE, truncate=True) if response.status_code == 200 else None
        except Exception as exc:
            log.info("인용 이미지 페이지 열기 실패 %s: %s", page_url, exc)
            return ""
        if not body:
            return ""
        return og_image(body.decode("utf-8", errors="ignore"), str(response.url or page_url))

    def _download(self, url: str) -> bytes | None:
        if not url:
            return None
        try:
            response = self._session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
            content_type = (response.headers.get("Content-Type") or response.headers.get("content-type") or "").lower()
            if response.status_code != 200 or not content_type.startswith("image/") or "svg" in content_type:
                return None
            data = _read_capped(response, MAX_DOWNLOAD)
        except Exception as exc:
            log.info("인용 이미지 내려받기 실패 %s: %s", url, exc)
            return None
        return data or None

    @staticmethod
    def _save(data: bytes, stem: Path) -> tuple[Path, int, int] | None:
        """크기·비율 확인 후 본문 폭에 맞게 줄여 저장 (이미지가 아니면 None)."""
        try:
            with Image.open(io.BytesIO(data)) as opened:
                opened.load()
                image = opened.copy()
        except Exception:
            return None
        width, height = image.size
        if width < MIN_WIDTH or height < MIN_HEIGHT or max(width / height, height / width) > MAX_RATIO:
            log.info("인용 이미지 제외 (크기 %dx%d)", width, height)
            return None
        image.thumbnail((MAX_WIDTH, MAX_WIDTH * 2))
        stem.parent.mkdir(parents=True, exist_ok=True)
        if _has_alpha(image):
            path = stem.with_suffix(".png")
            image.convert("RGBA").save(path, "PNG", optimize=True)
        else:
            path = stem.with_suffix(".jpg")
            image.convert("RGB").save(path, "JPEG", quality=88, optimize=True)
        return path, image.size[0], image.size[1]
