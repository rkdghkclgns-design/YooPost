"""참고 블로그(fornewchallenge.tistory.com) 전체 게시물을 확인해 글 '문법'(구성·서식) 프로필을 만든다.

    python scripts/analyze_reference_blog.py            # 전체 게시물 분석 (캐시 재사용, 새 글만 내려받음)
    python scripts/analyze_reference_blog.py --limit 50 # 최근 50개만 (빠른 확인용)

- 사이트맵의 모든 /entry/ 게시물을 1초 간격으로 내려받아 output/reference_cache/ 에 캐시한다 (robots.txt 준수).
- 본문을 복사해 저장하지 않는다. 구성 통계(소제목 패턴·박스·표·코드블록·FAQ·맺음말·참고문헌 등)만
  autopost/data/style_profile.json 으로 저장하고, AI 원고 프롬프트가 이 프로필을 따른다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://fornewchallenge.tistory.com"
SITEMAP = f"{BASE}/sitemap.xml"
ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "output" / "reference_cache"
PROFILE_PATH = ROOT / "autopost" / "data" / "style_profile.json"
DELAY_SECONDS = 1.0
RECENT_POSTS = 120  # 최근 문법(현재 스타일) 기준으로 삼을 게시물 수
USER_AGENT = "Mozilla/5.0 (compatible; YooPost-style-research/1.0)"
EMOJI_RE = re.compile(
    "^([\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿️‍]+)\\s*"
)
CLOSING_PHRASES = (
    "지금까지",
    "알아보았습니다",
    "다음 시간에 더 유익한 정보를 가지고 다시 찾아뵙겠습니다",
    "감사합니다",
    "직접 체험해보시길",
    "추천드리면서",
)
OPENING_PHRASES = ("안녕하세요!", "안녕하세요.", "함께 살펴보시죠", "알아보겠습니다", "다뤄보겠습니다")
BASIS_NOTE = re.compile(r"본 글은 .{0,20}기준으로 작성")


@dataclass
class PostStats:
    url: str
    title: str = ""
    category: str = ""
    published: str = ""
    chars: int = 0
    h2: list[str] = field(default_factory=list)
    h2_patterns: list[str] = field(default_factory=list)
    h3_count: int = 0
    images: int = 0
    tables: int = 0
    code_blocks: int = 0
    info_boxes: int = 0
    warning_boxes: int = 0
    summary_box: bool = False
    faq: bool = False
    faq_count: int = 0
    closing: bool = False
    references: bool = False
    reference_count: int = 0
    basis_note: bool = False
    related_posts: bool = False
    title_emoji: str = ""
    opening: str = ""
    closing_line: str = ""


def fetch(session: requests.Session, url: str, use_cache: bool = True) -> str:
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    path = CACHE_DIR / f"{key}.html"
    if use_cache and path.is_file():
        return path.read_text(encoding="utf-8")
    response = session.get(url, timeout=30)
    response.raise_for_status()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(response.text, encoding="utf-8")
    time.sleep(DELAY_SECONDS)
    return response.text


def post_urls(session: requests.Session) -> list[str]:
    xml = session.get(SITEMAP, timeout=30).text
    return [u for u in re.findall(r"<loc>([^<]+)</loc>", xml) if "/entry/" in u]


def split_emoji(text: str) -> tuple[str, str]:
    match = EMOJI_RE.match(text)
    if not match:
        return "", text.strip()
    return match.group(1).replace("️", ""), text[match.end():].strip()


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in re.split(r"[\s:·,/()\[\]\-–—|!?\"'“”]+", text) if len(t) >= 2}


def heading_pattern(heading: str, title: str) -> str:
    """소제목에서 글마다 달라지는 주제어(제목에 들어간 단어)를 빼고 공통 패턴만 남긴다.

    예) '📰 LongCat-2.5-Preview 등장 배경 - 메이투안의 야심' → '📰 등장 배경 - 메이투안의 야심'
    """
    emoji, rest = split_emoji(heading)
    title_words = _tokens(title)

    def topic_word(word: str) -> bool:  # 'LongCat-2.5-Preview' 처럼 제목 단어로만 이뤄진 어절
        parts = _tokens(word)
        return bool(parts) and parts <= title_words

    kept = [w for w in rest.split() if not topic_word(w)]
    pattern = re.sub(r"\s+", " ", " ".join(kept)).strip(" -:·")
    pattern = re.sub(r"^[-:·]\s*", "", pattern)
    return f"{emoji} {pattern}".strip() if emoji else pattern


def parse_post(url: str, html: str) -> PostStats | None:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one(".tt_article_useless_p_margin") or soup.select_one(".entry-content")
    if body is None:
        return None
    meta = lambda prop: (soup.select_one(f"meta[property='{prop}']") or {}).get("content", "")  # noqa: E731
    title = meta("og:title") or (soup.title.string if soup.title else "")
    stats = PostStats(url=url, title=title.strip())
    stats.published = meta("article:published_time")[:10]
    category = soup.select_one(".category a, .tit_category a, a.category, .another_category h4 a")
    stats.category = (category.get_text(strip=True) if category else "") or meta("article:section")
    stats.chars = len(re.sub(r"\s", "", body.get_text()))
    stats.h2 = [h.get_text(" ", strip=True) for h in body.find_all("h2")]
    stats.h2_patterns = [heading_pattern(h, stats.title) for h in stats.h2]
    stats.h3_count = len(body.find_all("h3"))
    stats.images = len(body.find_all("img"))
    stats.tables = len(body.find_all("table"))
    stats.code_blocks = len(body.select("div.code-block")) or len(body.find_all("pre"))
    stats.info_boxes = len(body.select("div.info"))
    stats.warning_boxes = len(body.select("div.warning"))
    summary = body.select_one("div.concept-block")
    stats.summary_box = bool(summary and "한눈에" in summary.get_text())
    text = body.get_text("\n", strip=True)
    stats.faq = any("자주 묻는 질문" in h or "FAQ" in h for h in stats.h2)
    stats.faq_count = len(re.findall(r"(?m)^Q\d+[.)]", text))
    stats.closing = any("맺음말" in h or "마치며" in h or "결론" in h for h in stats.h2)
    stats.references = any("참고" in h or "출처" in h for h in stats.h2)
    if stats.references:
        last_list = body.find_all("ul")
        stats.reference_count = len(last_list[-1].find_all("li")) if last_list else 0
    stats.basis_note = bool(BASIS_NOTE.search(text))
    stats.related_posts = any("함께 보면" in h for h in stats.h2)
    stats.title_emoji = split_emoji(stats.title)[0]
    paragraphs = [p.get_text(" ", strip=True) for p in body.find_all("p") if p.get_text(strip=True)]
    stats.opening = paragraphs[0][:80] if paragraphs else ""
    closers = [p for p in paragraphs if any(k in p for k in CLOSING_PHRASES)]
    stats.closing_line = closers[-1][-120:] if closers else ""
    return stats


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def _share(posts: list[PostStats], attr: str) -> float:
    return round(sum(1 for p in posts if getattr(p, attr)) / len(posts), 2) if posts else 0.0


def summarize(posts: list[PostStats]) -> dict:
    return {
        "posts": len(posts),
        "median_chars_no_space": _median([p.chars for p in posts]),
        "median_h2": _median([len(p.h2) for p in posts]),
        "median_h3": _median([p.h3_count for p in posts]),
        "median_images": _median([p.images for p in posts]),
        "median_tables": _median([p.tables for p in posts]),
        "median_code_blocks": _median([p.code_blocks for p in posts]),
        "median_info_boxes": _median([p.info_boxes for p in posts]),
        "median_warning_boxes": _median([p.warning_boxes for p in posts]),
        "median_faq_questions": _median([p.faq_count for p in posts if p.faq]),
        "median_references": _median([p.reference_count for p in posts if p.references]),
        "share_summary_box": _share(posts, "summary_box"),
        "share_faq": _share(posts, "faq"),
        "share_closing": _share(posts, "closing"),
        "share_references": _share(posts, "references"),
        "share_basis_note": _share(posts, "basis_note"),
        "share_related_posts": _share(posts, "related_posts"),
        "share_title_emoji": _share(posts, "title_emoji"),
        "top_title_emoji": Counter(p.title_emoji for p in posts if p.title_emoji).most_common(12),
        "top_h2_emoji": Counter(
            split_emoji(h)[0] for p in posts for h in p.h2 if split_emoji(h)[0]
        ).most_common(20),
        "top_h2_patterns": Counter(pat for p in posts for pat in p.h2_patterns if pat).most_common(40),
        "opening_phrases": {k: sum(1 for p in posts if k in p.opening) for k in OPENING_PHRASES},
        "closing_phrases": {k: sum(1 for p in posts if k in p.closing_line) for k in CLOSING_PHRASES},
    }


def build_profile(posts: list[PostStats]) -> dict:
    dated = sorted(posts, key=lambda p: p.published or "", reverse=True)
    recent = dated[:RECENT_POSTS]
    by_category: dict[str, list[PostStats]] = defaultdict(list)
    for post in recent:
        by_category[post.category or "기타"].append(post)
    return {
        "source": BASE,
        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
        "total_posts": len(posts),
        "date_range": [dated[-1].published if dated else "", dated[0].published if dated else ""],
        "categories": Counter(p.category or "기타" for p in posts).most_common(),
        "posts_per_year": sorted(Counter((p.published or "????")[:4] for p in posts).items()),
        "all": summarize(posts),
        "recent": summarize(recent),
        "recent_by_category": {
            name: {
                "posts": len(items),
                "top_h2_patterns": Counter(pat for p in items for pat in p.h2_patterns if pat).most_common(15),
                "median_chars_no_space": _median([p.chars for p in items]),
            }
            for name, items in sorted(by_category.items(), key=lambda kv: -len(kv[1]))
        },
        "recent_examples": [
            {"title": p.title, "category": p.category, "h2": p.h2} for p in recent[:8]
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="참고 블로그 전체 게시물 문법 분석")
    parser.add_argument("--limit", type=int, default=0, help="앞에서부터 N개만 (0=전체)")
    parser.add_argument("--no-cache", action="store_true", help="캐시 무시하고 다시 내려받기")
    parser.add_argument("--stats-out", default=str(ROOT / "output" / "reference_posts.json"))
    args = parser.parse_args(argv)

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    urls = post_urls(session)
    if args.limit:
        urls = urls[: args.limit]
    print(f"사이트맵 게시물 {len(urls)}개 분석 시작", flush=True)

    posts: list[PostStats] = []
    failed: list[str] = []
    for index, url in enumerate(urls, start=1):
        try:
            stats = parse_post(url, fetch(session, url, use_cache=not args.no_cache))
        except Exception as exc:  # 한 글 실패가 전체 분석을 멈추지 않도록
            failed.append(f"{url} ({exc})")
            continue
        if stats is not None:
            posts.append(stats)
        if index % 50 == 0:
            print(f"  {index}/{len(urls)} 처리", flush=True)

    profile = build_profile(posts)
    profile["failed"] = len(failed)
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=1), encoding="utf-8")
    stats_out = Path(args.stats_out)
    stats_out.parent.mkdir(parents=True, exist_ok=True)
    stats_out.write_text(json.dumps([asdict(p) for p in posts], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"완료: 분석 {len(posts)}개 · 실패 {len(failed)}개 → {PROFILE_PATH}", flush=True)
    for line in failed[:20]:
        print("  실패:", line)
    return 0 if posts else 1


if __name__ == "__main__":
    sys.exit(main())
