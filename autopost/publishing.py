"""발행 단계 — 참고문헌 붙이기, 미리보기, 구글 블로그 발행.

이미지(대표·인용)는 구글 드라이브가 아니라 이미지 저장소(Cloudflare Worker)에 올려 공개 주소로 넣는다.
저장소가 설정되지 않았거나 업로드에 실패하면 이미지를 본문에 직접 담아(data URI) 발행은 계속한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from . import blog_publisher
from .blog_html import ImageRef, PostExtras, build_blog_html
from .blog_publisher import BloggerPublisher, build_labels
from .constants import BASIS_NOTE, FACT_NOTE, RELATED_POSTS
from .content import PostContent, Reference
from .history import MAX_MARKER_SOURCES, PostHistory
from .image_host import ImageHost
from .preview_page import data_uri, render_preview, write_page
from .research import FactSheet, QuoteImage, pick_sources, reference_date
from .results import PublishResult

log = logging.getLogger(__name__)

PLATFORM_KEYS = {blog_publisher.PLATFORM: "blogger"}
INLINE_IMAGE_PX = 1000  # 저장소를 못 쓸 때 본문에 직접 담는 이미지 최대 크기 (글 용량이 너무 커지지 않게)


@dataclass(frozen=True)
class PublishContext:
    content: PostContent
    cover: Path | None
    notes: PostExtras  # basis_note·fact_note 만 채운 공통 부가 정보
    history: PostHistory
    stamp: str
    images: tuple[QuoteImage, ...] = ()  # 확인된 인용 이미지
    topic: str = ""  # 발행 기록용 (원 기사·발표 제목)
    source_url: str = ""
    sources: tuple[str, ...] = ()  # 발행 기록용 출처 주소 — 다른 매체로 같은 소식을 다시 쓰는 것을 막는 데 쓴다

    def related(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.history.related("blogger", self.content.category, RELATED_POSTS))

    def used_images(self) -> tuple[QuoteImage, ...]:
        """원고가 실제로 넣은 인용 이미지만 (올리지 않아도 되는 이미지는 건드리지 않는다)."""
        wanted = {b.image_id for s in self.content.sections for b in s.blocks if b.kind == "image"}
        return tuple(i for i in self.images if i.id in wanted)

    def marker(self) -> dict[str, object]:
        data: dict[str, object] = {"topic": self.topic or self.content.title, "source": self.source_url,
                                   "category": self.content.category}
        return {**data, "sources": list(self.sources[:MAX_MARKER_SOURCES])} if self.sources else data


def attach_references(content: PostContent, sheet: FactSheet, now: datetime) -> PostContent:
    """원고가 인용한 출처(확인된 것만)를 APA 형식 참고문헌으로 붙인다 — 참고문헌 주소를 AI 가 지어낼 수 없다."""
    references = [
        Reference(publisher=s.publisher, date=reference_date(s.published, str(now.year)), title=s.title, url=s.url)
        for s in pick_sources(sheet, content.source_ids)
    ]
    return content.with_references(references)


def base_notes(now: datetime, fact_checked: bool) -> PostExtras:
    return PostExtras(
        basis_note=BASIS_NOTE.format(date=f"{now.year}년 {now.month}월 {now.day}일"),
        fact_note=FACT_NOTE if fact_checked else "",
    )


def write_preview(ctx: PublishContext, workdir: Path, dry_run: bool) -> Path | None:
    """검토용: 구글 블로그에 올라갈 모습 그대로의 페이지 (실패해도 포스팅은 계속)."""
    note = "검토용 미리보기 — 아직 어디에도 게시되지 않았습니다." if dry_run else "발행 원고 미리보기 사본입니다."
    try:
        images = tuple(ImageRef(i.id, data_uri(i.path), i.caption, i.credit, i.page_url) for i in ctx.used_images())
        page = render_preview(ctx.content, replace(ctx.notes, related=ctx.related(), images=images), ctx.cover, note)
        return write_page(workdir / "blog_preview_google.html", page)
    except Exception:
        log.exception("블로그 미리보기 생성 실패")
        return None


def _public_url(host: ImageHost | None, path: Path, name: str) -> str:
    """이미지 저장소에 올린 주소 — 저장소가 없거나 실패하면 본문에 직접 담는다 (발행은 계속)."""
    if host is not None:
        try:
            return host.upload(path, name)
        except Exception as exc:
            log.warning("이미지 저장소 업로드 실패 — 본문에 직접 담습니다: %s", exc)
    return data_uri(path, INLINE_IMAGE_PX)


def build_publish_html(host: ImageHost | None, ctx: PublishContext) -> str:
    """이미지를 저장소에 올리고 구글 블로그에 그대로 올릴 본문 HTML 을 만든다 (발행 기록 표시 포함)."""
    cover_url = None
    if ctx.cover is not None:
        cover_url = _public_url(host, ctx.cover, f"{ctx.stamp}_cover{ctx.cover.suffix}")
    images = tuple(
        ImageRef(i.id, _public_url(host, i.path, f"{ctx.stamp}_{i.id}{i.path.suffix}"), i.caption, i.credit,
                 i.page_url)
        for i in ctx.used_images()
    )
    extras = replace(ctx.notes, cover_url=cover_url, related=ctx.related(), images=images, marker=ctx.marker())
    return build_blog_html(ctx.content, extras)


def publish_blogger(host: ImageHost | None, blogger: BloggerPublisher | None, ctx: PublishContext) -> PublishResult:
    """대표 이미지·인용 이미지를 저장소에 올린 뒤 구글 블로그에 발행 (실패는 결과로 돌려준다)."""
    if blogger is None:
        return PublishResult.skip(blog_publisher.PLATFORM, "BLOGGER_BLOG_ID 미설정")
    try:
        body = build_publish_html(host, ctx)
        return blogger.publish(ctx.content.title, body, build_labels(ctx.content.category, ctx.content.hashtags))
    except Exception as exc:
        log.exception("%s 발행 실패", blog_publisher.PLATFORM)
        return PublishResult.fail(blog_publisher.PLATFORM, exc)


def skipped_results(reason: str) -> list[PublishResult]:
    return [PublishResult.skip(blog_publisher.PLATFORM, reason)]


def published_urls(results) -> dict[str, str]:
    return {PLATFORM_KEYS[r.platform]: r.url for r in results if r.ok and r.url and r.platform in PLATFORM_KEYS}
