"""발행 단계 — 참고문헌 붙이기, 미리보기, 구글 블로그·네이버 발행 (한 플랫폼 실패가 다른 플랫폼을 막지 않음)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import blog_publisher, naver_publisher
from .blog_html import PostExtras, build_blog_html
from .blog_publisher import BloggerPublisher, build_labels
from .constants import BASIS_NOTE, FACT_NOTE, RELATED_POSTS
from .content import PostContent, Reference
from .drive_helper import public_image_url
from .history import PostHistory
from .naver_publisher import NaverExtras, NaverPublisher
from .preview_page import render_naver_preview, render_preview, write_page
from .research import FactSheet, pick_sources, reference_date
from .results import PublishResult

log = logging.getLogger(__name__)

PLATFORM_KEYS = {blog_publisher.PLATFORM: "blogger", naver_publisher.PLATFORM: "naver"}


@dataclass(frozen=True)
class PublishContext:
    content: PostContent
    cover: Path | None
    notes: PostExtras  # basis_note·fact_note 만 채운 공통 부가 정보
    history: PostHistory
    public_folder_id: str
    stamp: str


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


def notes_text(content: PostContent, notes: PostExtras) -> str:
    return " ".join(n for n in (notes.basis_note, content.caveat, notes.fact_note) if n)


def write_previews(ctx: PublishContext, workdir: Path, blog_name: str, align: str, dry_run: bool) -> tuple[Path | None, Path | None]:
    """검토용: 구글·네이버 블로그에 올라갈 모습 그대로의 페이지 (실패해도 포스팅은 계속)."""
    note = "검토용 미리보기 — 아직 어디에도 게시되지 않았습니다." if dry_run else "발행 원고 미리보기 사본입니다."
    google = replace(ctx.notes, related=tuple(ctx.history.related("blogger", ctx.content.category, RELATED_POSTS)))
    naver = NaverExtras(ctx.cover, tuple(ctx.history.related("naver", ctx.content.category, RELATED_POSTS)),
                        notes_text(ctx.content, ctx.notes))
    pages = (
        (lambda: render_preview(ctx.content, google, ctx.cover, note), "blog_preview_google.html"),
        (lambda: render_naver_preview(ctx.content, naver, note, f"{blog_name} (네이버)", align), "blog_preview_naver.html"),
    )
    written: list[Path | None] = []
    for render, name in pages:
        try:
            written.append(write_page(workdir / name, render()))
        except Exception:
            log.exception("블로그 미리보기 생성 실패: %s", name)
            written.append(None)
    return written[0], written[1]


def _safely(platform: str, action: Callable[[], PublishResult]) -> PublishResult:
    try:
        return action()
    except Exception as exc:  # 한 플랫폼 실패가 다른 플랫폼 발행을 막지 않도록
        log.exception("%s 발행 실패", platform)
        return PublishResult.fail(platform, exc)


def _publish_blogger(drive, blogger: BloggerPublisher, ctx: PublishContext) -> PublishResult:
    cover_url = None
    if ctx.cover is not None:
        uploaded = drive.upload(ctx.public_folder_id, ctx.cover, name=f"{ctx.stamp}_cover.png")
        drive.make_public(uploaded.id)
        cover_url = public_image_url(uploaded.id)
    related = tuple(ctx.history.related("blogger", ctx.content.category, RELATED_POSTS))
    body = build_blog_html(ctx.content, replace(ctx.notes, cover_url=cover_url, related=related))
    return blogger.publish(ctx.content.title, body, build_labels(ctx.content.category, ctx.content.hashtags))


def _publish_naver(naver: NaverPublisher, ctx: PublishContext) -> PublishResult:
    related = tuple(ctx.history.related("naver", ctx.content.category, RELATED_POSTS))
    return naver.publish(ctx.content, NaverExtras(ctx.cover, related, notes_text(ctx.content, ctx.notes)))


def publish_all(drive, blogger: BloggerPublisher | None, naver: NaverPublisher | None, ctx: PublishContext) -> list[PublishResult]:
    results: list[PublishResult] = []
    if blogger is None:
        results.append(PublishResult.skip(blog_publisher.PLATFORM, "BLOGGER_BLOG_ID 미설정"))
    else:
        results.append(_safely(blog_publisher.PLATFORM, lambda: _publish_blogger(drive, blogger, ctx)))
    if naver is None:
        results.append(PublishResult.skip(naver_publisher.PLATFORM, "NAVER_* 미설정"))
    else:
        results.append(_safely(naver_publisher.PLATFORM, lambda: _publish_naver(naver, ctx)))
    return results


def skipped_results(reason: str) -> list[PublishResult]:
    return [PublishResult.skip(blog_publisher.PLATFORM, reason), PublishResult.skip(naver_publisher.PLATFORM, reason)]


def published_urls(results) -> dict[str, str]:
    return {PLATFORM_KEYS[r.platform]: r.url for r in results if r.ok and r.url and r.platform in PLATFORM_KEYS}
