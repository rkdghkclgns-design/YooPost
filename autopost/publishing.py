"""발행 단계 — 참고문헌 붙이기, 미리보기, 구글 블로그 발행."""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from . import blog_publisher
from .blog_html import PostExtras, build_blog_html
from .blog_publisher import BloggerPublisher, build_labels
from .constants import BASIS_NOTE, FACT_NOTE, RELATED_POSTS
from .content import PostContent, Reference
from .drive_helper import public_image_url
from .history import PostHistory
from .preview_page import render_preview, write_page
from .research import FactSheet, pick_sources, reference_date
from .results import PublishResult

log = logging.getLogger(__name__)

PLATFORM_KEYS = {blog_publisher.PLATFORM: "blogger"}


@dataclass(frozen=True)
class PublishContext:
    content: PostContent
    cover: Path | None
    notes: PostExtras  # basis_note·fact_note 만 채운 공통 부가 정보
    history: PostHistory
    public_folder_id: str
    stamp: str

    def related(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.history.related("blogger", self.content.category, RELATED_POSTS))


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
        page = render_preview(ctx.content, replace(ctx.notes, related=ctx.related()), ctx.cover, note)
        return write_page(workdir / "blog_preview_google.html", page)
    except Exception:
        log.exception("블로그 미리보기 생성 실패")
        return None


def publish_blogger(drive, blogger: BloggerPublisher | None, ctx: PublishContext) -> PublishResult:
    """대표 이미지를 드라이브에 공개 업로드한 뒤 구글 블로그에 발행 (실패는 결과로 돌려준다)."""
    if blogger is None:
        return PublishResult.skip(blog_publisher.PLATFORM, "BLOGGER_BLOG_ID 미설정")
    try:
        cover_url = None
        if ctx.cover is not None:
            uploaded = drive.upload(ctx.public_folder_id, ctx.cover, name=f"{ctx.stamp}_cover.png")
            drive.make_public(uploaded.id)
            cover_url = public_image_url(uploaded.id)
        body = build_blog_html(ctx.content, replace(ctx.notes, cover_url=cover_url, related=ctx.related()))
        return blogger.publish(ctx.content.title, body, build_labels(ctx.content.category, ctx.content.hashtags))
    except Exception as exc:
        log.exception("%s 발행 실패", blog_publisher.PLATFORM)
        return PublishResult.fail(blog_publisher.PLATFORM, exc)


def skipped_results(reason: str) -> list[PublishResult]:
    return [PublishResult.skip(blog_publisher.PLATFORM, reason)]


def published_urls(results) -> dict[str, str]:
    return {PLATFORM_KEYS[r.platform]: r.url for r in results if r.ok and r.url and r.platform in PLATFORM_KEYS}
