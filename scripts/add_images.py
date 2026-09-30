"""발행 글의 본문 그림을 원고와 관련된 공식 출처 본문 그림으로 맞춘다 — 원고 폴더(출처 목록)가 남아 있는 글만.

    python scripts/add_images.py --latest            # 가장 최근 글만 미리 보기 (아무것도 바꾸지 않음)
    python scripts/add_images.py --latest --apply    # 가장 최근 글만 적용
    python scripts/add_images.py [--apply]           # 모든 글

- 예전에 넣은 대표 이미지(og:image 썸네일, 이미지 키에 _auto_)는 뺀다 — 원고와 관련 없는 공유 카드였다.
- 새로 넣는 그림은 공식 출처 페이지 본문(머리글·메뉴·바닥글 제외)의 그림 중 설명이 원고 낱말과 겹치는 것만.
  썸네일·로고·공유 카드는 쓰지 않고, 관련 그림이 없으면 그림 없이 둔다.
- 그림은 설명과 가장 관련 있는 소제목의 첫 문단 뒤에 넣는다. 캡션에 출처·저작권자를 밝힌다.
원고는 yoopost-drafts 브랜치(.yoopost-drafts/drafts)와 로컬(output/claude_drafts)에서 제목·주제로 찾는다.
AI 호출 없음. 필요(.env): GOOGLE_*·BLOGGER_BLOG_ID, IMAGE_HOST_URL·IMAGE_UPLOAD_TOKEN.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from autopost.blog_html import ImageRef, figure_html  # noqa: E402
from autopost.blog_publisher import BloggerPublisher  # noqa: E402
from autopost.claude_draft import DraftError, draft_keywords, load_draft  # noqa: E402
from autopost.cli import http_session  # noqa: E402
from autopost.config import load_config  # noqa: E402
from autopost.google_auth import build_blogger_service, build_credentials  # noqa: E402
from autopost.history import read_marker  # noqa: E402
from autopost.image_host import ImageHost, safe_key  # noqa: E402
from autopost.post_images import insert_figures, remove_auto_figures  # noqa: E402
from autopost.quote_images import AUTO_IMAGES, ImageCollector, auto_candidates  # noqa: E402
from autopost.writer import best_sections  # noqa: E402

DRAFT_DIRS = (ROOT / ".yoopost-drafts" / "drafts", ROOT / "output" / "claude_drafts")


def draft_index() -> dict[str, Path]:
    """제목·주제 → 원고 폴더 (같은 제목이면 나중 원고)."""
    index: dict[str, Path] = {}
    for base in DRAFT_DIRS:
        for folder in sorted(p for p in base.glob("*_*_*") if p.is_dir()):
            try:
                files = load_draft(folder)
            except DraftError:
                continue
            index[files.content.title.strip()] = folder
            if files.topic:
                index[files.topic.strip()] = folder
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="발행 글 본문 그림을 원고와 관련된 공식 출처 본문 그림으로 맞추기")
    parser.add_argument("--apply", action="store_true", help="실제로 바꾼다 (없으면 미리 보기)")
    parser.add_argument("--latest", action="store_true", help="가장 최근에 발행한 글 하나만")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    cfg = load_config()
    if cfg.google is None or not cfg.blogger_blog_id or not cfg.image_host_enabled:
        print("❌ .env 에 GOOGLE_*·BLOGGER_BLOG_ID·IMAGE_HOST_URL·IMAGE_UPLOAD_TOKEN 이 필요합니다")
        return 2
    session = http_session()
    blogger = BloggerPublisher(build_blogger_service(build_credentials(cfg.google)), cfg.blogger_blog_id)
    host = ImageHost(cfg.image_host_url, cfg.image_upload_token, session)
    collector = ImageCollector(session)
    index = draft_index()
    tmp = Path(tempfile.mkdtemp())
    changed = skipped = 0
    mode = "적용" if args.apply else "미리 보기"
    print(f"== 발행 글 본문 그림 교정 ({mode}{' · 가장 최근 글만' if args.latest else ''})")
    for post in blogger.editable_posts(limit=1 if args.latest else 200):
        content = str(post.get("content") or "")
        title = str(post.get("title") or "").strip()
        cleaned, removed = remove_auto_figures(content)
        print(f"  📄 {title[:60]} — {post.get('url', '')}")
        if "<figure" in cleaned:
            print("     ⏭️ 원고 작성 때 넣은 인용 그림이 있어 그대로 둠")
            skipped += 1
            continue
        folder = index.get(title) or index.get(str(read_marker(content).get("topic") or "").strip())
        figures: list[str] = []
        places: list[int] = []
        sections = 0
        if removed:
            print(f"     🧹 예전에 넣은 대표 이미지(썸네일) {removed}개 제거")
        if folder is None:
            print("     ⚠️ 원고 폴더가 없어 출처를 알 수 없음 — 관련 그림은 넣지 못함")
        else:
            files = load_draft(folder)
            work = tmp / folder.name
            work.mkdir(parents=True, exist_ok=True)
            images = collector.collect(auto_candidates(files.sources), files.sources, work,
                                       keywords=draft_keywords(files))[:AUTO_IMAGES]
            if not images:
                print("     ⏭️ 공식 출처 본문에 원고와 관련된 그림이 없음 — 그림 없이 둠")
            sections = len(files.content.sections)
            places = best_sections(files.content, [i.caption for i in images])
            for image, place in zip(images, places):
                heading = files.content.sections[place].heading if place < sections else "?"
                print(f"     🖼️ {image.id} «{image.caption}» ({image.credit} {image.width}x{image.height})"
                      f" → 소제목 {place + 1} «{heading}»\n        파일: {image.path}")
                if args.apply:
                    url = host.upload(image.path, safe_key(f"{folder.name}_rel_{image.id}{image.path.suffix}"))
                    figures.append(figure_html(ImageRef(image.id, url, image.caption, image.credit, image.page_url)))
        html = insert_figures(cleaned, figures, sections, places) if figures else cleaned
        if html == content:
            skipped += 1
            continue
        changed += 1
        if args.apply:
            blogger.update_content(str(post["id"]), html)
            print("     ✅ 본문 수정 완료")
    print(f"{mode}: 바꿀 글 {changed} · 그대로 {skipped}" + ("" if args.apply else " — 적용하려면 --apply"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
