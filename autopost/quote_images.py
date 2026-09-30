"""인용 이미지 확보 — 원고 내용과 관련된 **본문 그림**(도표·화면·다이어그램)만 내려받아 확인한다.

통과 기준 (하나라도 어기면 버린다 — 관련 그림이 없으면 이미지를 넣지 않는다):
  - 이미지가 실린 페이지가 조사에서 확인된 출처와 같은 사이트 (blogs.nvidia.com ↔ nvidianews.nvidia.com 은 같은 곳으로 봄)
  - 공유용 대표 이미지(og:image·twitter:image)·로고·아이콘·썸네일·아바타는 쓰지 않는다 (2026-09-30 사용자 지시 — 썸네일 금지)
  - 주소를 모르면 출처 페이지 본문(머리글·메뉴·바닥글·사이드바 제외)의 <img> 가운데 설명(alt·캡션·앞 소제목)이
    원고 내용(제목·소제목·주제·캡션)과 겹치는 그림만 고른다 — 겹치는 말이 없으면 넣지 않는다
  - 실제로 내려받아지는 이미지 파일, 너무 작지 않음, 극단적으로 길쭉하지 않음, 같은 이미지 중복 제거
내려받은 이미지는 본문 폭에 맞게 줄이고 JPEG(투명 배경이면 PNG)로 저장한다.
"""
from __future__ import annotations

import hashlib
import html
import io
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urljoin, urlparse

from PIL import Image

from .links import TIMEOUT, domain, is_http_url
from .research import ImageCandidate, QuoteImage, Source

log = logging.getLogger(__name__)

MAX_IMAGES = 3
MAX_DOWNLOAD = 8 * 1024 * 1024
MAX_PAGE = 1_500_000  # 본문 그림을 찾으려면 페이지를 넉넉히 읽는다
MIN_WIDTH = 480
MIN_HEIGHT = 200
MAX_RATIO = 4.0
MAX_WIDTH = 1400
MAX_CAPTION = 120
AUTO_IMAGES = 2  # 원고에 쓸 이미지가 없을 때 공식 출처 본문 그림에서 보충하는 최대 개수
AUTO_PAGES = 4  # 본문 그림을 찾아볼 공식 출처 페이지 수
MIN_OVERLAP = 2  # 그림 설명과 원고가 이만큼 겹쳐야 관련 그림 (figure 캡션이 있는 그림은 1)
_OG_IMAGE = (
    re.compile(r'<meta[^>]+(?:property|name)=["\']og:image(?::url)?["\'][^>]*content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:image(?::url)?["\']', re.I),
)
_META_NAMES = r"(?:og:image(?::url|:secure_url)?|twitter:image(?::src)?)"
_META_IMAGE = (
    re.compile(r'<meta[^>]+(?:property|name)=["\']' + _META_NAMES + r'["\'][^>]*content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']' + _META_NAMES + r'["\']', re.I),
)
# 썸네일·로고류 주소 (경로 조각 단위) — 공유 카드·아이콘은 본문과 관련된 그림이 아니다
_THUMB_URL = re.compile(
    r"(?:^|[/_\-.])(?:logos?|icons?|favicon|avatars?|sprites?|badges?|emoji|pixel|spacer|tracking|placeholder|"
    r"thumbs?|thumbnails?|og|ogimage|og-image|opengraph|social|share|sharing|twitter-card|meta-image|profile)"
    r"(?:[/_\-.@0-9]|$)", re.I)
_TOKEN = re.compile(r"[0-9a-z가-힣][0-9a-z가-힣.+#\-]*", re.I)
_STOPWORDS = frozenset("""
the and for with from this that these those your you are can how what when where which who why will has have had
not all one two new more most into about over under our its their them they his her was were been being use used
using via per also than then just only any each other some such image images screenshot screenshots figure fig
diagram photo picture pictures example examples click shown show shows page view see here there below above left
right top bottom step steps guide overview introduction section learn read menu button default setting settings
""".split())
SKIP_TAGS = frozenset({"header", "nav", "footer", "aside", "noscript", "script", "style", "form"})
SKIP_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary"})


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

    truncate=False 면 limit 을 넘는 본문은 버린다(None), True 면 앞부분만 쓴다 (HTML 본문 그림 찾기용).
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


# ------------------------------------------------------------------ 관련도 (그림 설명 ↔ 원고 낱말)
def tokens(text: str) -> set[str]:
    """관련도 비교용 낱말 — 소문자, 영어 3자·한글 2자 이상, 흔한 말·숫자만 있는 말 제외."""
    found = set()
    for raw in _TOKEN.findall((text or "").lower()):
        word = raw.strip(".-+#")
        if not word or word in _STOPWORDS or word.replace(".", "").isdigit():
            continue
        if len(word) >= 3 or (len(word) >= 2 and re.search(r"[가-힣]", word)):
            found.add(word)
    return found


def keywords_of(*texts: str) -> set[str]:
    return set().union(*(tokens(t) for t in texts)) if texts else set()


def brand_words(sources: Iterable[Source]) -> set[str]:
    """회사·사이트 이름 — 그림 설명에 들어 있어도 관련 근거로 세지 않는다 (로고·브랜드 카드 차단)."""
    words: set[str] = set()
    for source in sources:
        words |= tokens(source.publisher)
        words |= tokens(site(source.url).split(".")[0])
    return words


def is_thumbnail_url(url: str) -> bool:
    """공유 카드·로고·아이콘·썸네일처럼 보이는 이미지 주소."""
    return bool(_THUMB_URL.search(urlparse(url).path or ""))


def meta_images(page_html: str, page_url: str) -> set[str]:
    """페이지가 공유용으로 내건 대표 이미지(og:image·twitter:image) — 본문 그림으로 쓰지 않는다."""
    found = set()
    for pattern in _META_IMAGE:
        for match in pattern.finditer(page_html or ""):
            raw = html.unescape(match.group(1).strip())
            if raw:
                found.add(urljoin(page_url, raw))
    return found


def og_image(page_html: str, page_url: str) -> str:
    """페이지의 대표 이미지 주소 (본문 그림 후보에서 빼는 데 쓴다)."""
    for pattern in _OG_IMAGE:
        match = pattern.search(page_html)
        if match:
            url = urljoin(page_url, html.unescape(match.group(1).strip()))
            return url if is_http_url(url) else ""
    return ""


@dataclass(frozen=True)
class PageImage:
    """출처 페이지 본문의 그림 한 장 — 설명(alt·figure 캡션·앞 소제목)으로 관련도를 잰다."""
    url: str
    alt: str = ""
    caption: str = ""
    heading: str = ""

    @property
    def description(self) -> str:
        return " ".join(t for t in (self.caption, self.alt) if t)


def _largest(srcset: str, base: str) -> str:
    best, best_width = "", -1.0
    for part in (srcset or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        try:
            width = float((bits[1] if len(bits) > 1 else "1w").rstrip("wx"))
        except ValueError:
            width = 1.0
        if width > best_width:
            best, best_width = bits[0], width
    return urljoin(base, best) if best else ""


class _ArticleImages(HTMLParser):
    """머리글·메뉴·바닥글·사이드바 밖의 <img> — figure 안이면 figcaption 을 캡션으로, 앞 소제목을 함께 기억."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.skip: dict[str, int] = {}
        self.images: list[PageImage] = []
        self.heading = ""
        self._heading: list[str] | None = None
        self._figures: list[dict] = []
        self._caption = False
        self._source = ""

    def _skipping(self) -> bool:
        return any(count > 0 for count in self.skip.values())

    def handle_starttag(self, tag, attrs):
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag in SKIP_TAGS or attr.get("role", "").lower() in SKIP_ROLES:
            self.skip[tag] = self.skip.get(tag, 0) + 1
            return
        if self._skipping():
            return
        if tag in ("h1", "h2", "h3", "h4"):
            self._heading = []
        elif tag == "figure":
            self._figures.append({"images": [], "caption": []})
        elif tag == "figcaption":
            self._caption = True
        elif tag == "source":
            self._source = _largest(attr.get("srcset") or attr.get("data-srcset", ""), self.base) or self._source
        elif tag == "img":
            self._image(attr)

    def _image(self, attr: dict) -> None:
        src = attr.get("data-src") or attr.get("data-lazy-src") or attr.get("data-original") or attr.get("src") or ""
        url = (_largest(attr.get("srcset") or attr.get("data-srcset", ""), self.base)
               or (urljoin(self.base, src) if src and not src.startswith("data:") else "") or self._source)
        self._source = ""
        width = re.sub(r"\D", "", attr.get("width", ""))
        if not is_http_url(url) or (width and int(width) < MIN_WIDTH):
            return
        image = PageImage(url, " ".join(attr.get("alt", "").split())[:MAX_CAPTION * 2], "", self.heading)
        if self._figures:
            self._figures[-1]["images"].append(image)
        else:
            self.images.append(image)

    def handle_endtag(self, tag):
        if self.skip.get(tag, 0) > 0:
            self.skip[tag] -= 1
            return
        if self._skipping():
            return
        if tag in ("h1", "h2", "h3", "h4") and self._heading is not None:
            self.heading = " ".join("".join(self._heading).split())[:200]
            self._heading = None
        elif tag == "figcaption":
            self._caption = False
        elif tag == "figure" and self._figures:
            figure = self._figures.pop()
            caption = " ".join("".join(figure["caption"]).split())[:MAX_CAPTION * 2]
            self.images.extend(PageImage(i.url, i.alt, caption, i.heading) for i in figure["images"])

    def handle_data(self, data):
        if self._skipping():
            return
        if self._heading is not None:
            self._heading.append(data)
        if self._caption and self._figures:
            self._figures[-1]["caption"].append(data)


def article_images(page_html: str, page_url: str) -> list[PageImage]:
    """출처 페이지 본문 그림 — 공유용 대표 이미지·썸네일·로고류 주소는 빼고, 같은 주소는 한 번."""
    parser = _ArticleImages(page_url)
    try:
        parser.feed(page_html or "")
        parser.close()
    except Exception as exc:  # 깨진 HTML — 읽은 데까지만
        log.info("본문 그림 읽기 중단 %s: %s", page_url, exc)
    banned = meta_images(page_html, page_url)
    seen: set[str] = set()
    result: list[PageImage] = []
    for image in parser.images:
        if image.url in seen or image.url in banned or is_thumbnail_url(image.url):
            continue
        seen.add(image.url)
        result.append(image)
    return result


def relevance(image: PageImage, keywords: set[str], brand: set[str]) -> int:
    """그림 설명과 원고 낱말이 겹치는 수 (회사·사이트 이름 제외). 설명이 없는 그림은 0 — 관련을 확인할 수 없다."""
    described = tokens(image.description)
    if not described - brand:
        return 0
    return len(((described | tokens(image.heading)) - brand) & (keywords - brand))


def relevant_images(images: Sequence[PageImage], keywords: set[str], brand: set[str]) -> list[PageImage]:
    """관련도가 기준 이상인 그림을 관련도 높은 순으로 (figure 캡션이 있는 그림은 1개만 겹쳐도 인정)."""
    scored = [(relevance(image, keywords, brand), index, image) for index, image in enumerate(images)]
    kept = [(score, index, image) for score, index, image in scored
            if score >= (1 if image.caption else MIN_OVERLAP)]
    return [image for _, _, image in sorted(kept, key=lambda item: (-item[0], item[1]))]


def auto_candidates(sources: Sequence[Source]) -> tuple[ImageCandidate, ...]:
    """원고에 쓸 이미지가 하나도 없을 때 — 공식 출처 페이지에서 본문 그림을 찾아볼 후보 (캡션은 그림 설명으로).

    작성자가 이미지를 빠뜨려도(클라우드 원고는 자주 그랬다) 원고와 관련된 본문 그림이 있으면 넣는 보충 장치.
    공유용 대표 이미지(썸네일)는 쓰지 않는다 — 관련 그림이 없으면 아무것도 넣지 않는다.
    """
    result: list[ImageCandidate] = []
    pages: set[str] = set()
    for source in sources:
        if not source.official or not is_http_url(source.url) or source.url in pages:
            continue
        pages.add(source.url)
        name = site(source.url)
        result.append(ImageCandidate(id=f"A{len(result) + 1}", image_url="", page_url=source.url, caption="",
                                     credit=(" ".join((source.publisher or name).split()))[:60]))
        if len(result) >= AUTO_PAGES:
            break
    return tuple(result)


class ImageCollector:
    def __init__(self, session=None) -> None:
        if session is None:
            import requests

            session = requests.Session()
        self._session = session
        self._pages: dict[str, str] = {}

    def collect(self, candidates: Sequence[ImageCandidate], sources: Sequence[Source], workdir: Path,
                keywords: Iterable[str] = ()) -> tuple[QuoteImage, ...]:
        """확인을 통과한 인용 이미지. keywords = 원고 낱말 (제목·소제목·주제) — 본문 그림을 고를 때 관련도 기준."""
        allowed = {site(s.url) for s in sources}
        publishers = {site(s.url): s.publisher for s in sources}
        brand = brand_words(sources)
        words = set(keywords)
        kept: list[QuoteImage] = []
        hashes: set[str] = set()
        used: set[str] = set()
        for candidate in candidates:
            page_site = site(candidate.page_url or candidate.image_url)
            if page_site not in allowed:
                log.info("인용 이미지 제외 (확인된 출처 사이트가 아님): %s", candidate.page_url or candidate.image_url)
                continue
            found = self._fetch(candidate, words | tokens(candidate.caption), brand, hashes, used)
            if found is None:
                log.info("인용 이미지 제외 (관련 본문 그림 없음·내려받기 실패): %s", candidate.image_url or candidate.page_url)
                continue
            data, url, description = found
            saved = self._save(data, workdir / f"quote_{candidate.id}")
            if saved is None:
                continue
            hashes.add(hashlib.sha256(data).hexdigest())
            used.add(url)
            path, width, height = saved
            caption = candidate.caption or " ".join(description.split())[:MAX_CAPTION]
            credit = candidate.credit or publishers.get(page_site) or page_site
            kept.append(QuoteImage(candidate.id, caption, credit, candidate.page_url or candidate.image_url,
                                   path, width, height))
        return tuple(kept)

    def _fetch(self, candidate: ImageCandidate, words: set[str], brand: set[str], hashes: set[str],
               used: set[str]) -> tuple[bytes, str, str] | None:
        """(이미지 바이트, 주소, 그림 설명) — 작성자가 준 주소(썸네일이 아니면) → 없으면 출처 본문의 관련 그림."""
        page_html = self._page(candidate.page_url)
        banned = meta_images(page_html, candidate.page_url) if page_html else set()
        if candidate.image_url and not is_thumbnail_url(candidate.image_url) and candidate.image_url not in banned:
            data = self._download(candidate.image_url)
            if data is not None and hashlib.sha256(data).hexdigest() not in hashes:
                return data, candidate.image_url, candidate.caption
        if not page_html:
            return None
        for image in relevant_images(article_images(page_html, candidate.page_url), words, brand)[:4]:
            if image.url in used:
                continue
            data = self._download(image.url)
            if data is not None and hashlib.sha256(data).hexdigest() not in hashes:
                return data, image.url, image.description
        return None

    def _page(self, page_url: str) -> str:
        if not page_url:
            return ""
        if page_url in self._pages:
            return self._pages[page_url]
        text = ""
        try:
            response = self._session.get(page_url, timeout=TIMEOUT, allow_redirects=True, stream=True)
            body = _read_capped(response, MAX_PAGE, truncate=True) if response.status_code == 200 else None
            text = body.decode("utf-8", errors="ignore") if body else ""
        except Exception as exc:
            log.info("인용 이미지 페이지 열기 실패 %s: %s", page_url, exc)
        self._pages[page_url] = text
        return text

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
