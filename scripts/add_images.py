"""이미지 없이 발행된 글에 공식 출처의 대표 이미지(og:image)를 넣는다 — 원고 폴더(출처 목록)가 남아 있는 글만.

    python scripts/add_images.py            # 미리 보기 — 넣을 이미지만 확인 (아무것도 바꾸지 않음)
    python scripts/add_images.py --apply    # 적용 — 이미지 저장소에 올리고 발행 글 본문 수정

원고는 yoopost-drafts 브랜치(.yoopost-drafts/drafts)와 로컬(output/claude_drafts)에서 제목·주제로 찾는다.
이미지는 공식 출처(official) 페이지의 대표 이미지만, 캡션에 출처·저작권자를 밝힌다 (새 원고의 자동 보충과 같은 규칙).
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
from autopost.claude_draft import DraftError, load_draft  # noqa: E402
from autopost.cli import http_session  # noqa: E402
from autopost.config import load_config  # noqa: E402
from autopost.google_auth import build_blogger_service, build_credentials  # noqa: E402
from autopost.history import read_marker  # noqa: E402
from autopost.image_host import ImageHost, safe_key  # noqa: E402
from autopost.post_images import insert_figures  # noqa: E402
from autopost.quote_images import AUTO_IMAGES, ImageCollector, auto_candidates  # noqa: E402

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
    parser = argparse.ArgumentParser(description="이미지 없이 발행된 글에 공식 출처 대표 이미지 넣기")
    parser.add_argument("--apply", action="store_true", help="실제로 바꾼다 (없으면 미리 보기)")
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
    print(f"== 이미지 없는 발행 글 ({'적용' if args.apply else '미리 보기'})")
    for post in blogger.editable_posts():
        content = str(post.get("content") or "")
        title = str(post.get("title") or "").strip()
        if "<figure" in content:
            continue
        folder = index.get(title) or index.get(str(read_marker(content).get("topic") or "").strip())
        if folder is None:
            print(f"  ⏭️ {title[:60]} — 원고 폴더가 없어 출처를 알 수 없음")
            skipped += 1
            continue
        files = load_draft(folder)
        work = tmp / folder.name
        work.mkdir(parents=True, exist_ok=True)
        images = collector.collect(auto_candidates(files.sources), files.sources, work)[:AUTO_IMAGES]
        if not images:
            print(f"  ⏭️ {title[:60]} — 공식 출처에 쓸 대표 이미지가 없음")
            skipped += 1
            continue
        print(f"  🖼️ {title[:60]} — {len(images)}개: " + ", ".join(f"{i.credit} {i.width}x{i.height}" for i in images))
        if not args.apply:
            changed += 1
            continue
        figures = []
        for image in images:
            url = host.upload(image.path, safe_key(f"{folder.name}_auto_{image.id}{image.path.suffix}"))
            figures.append(figure_html(ImageRef(image.id, url, image.caption, image.credit, image.page_url)))
        html = insert_figures(content, figures, len(files.content.sections))
        if html != content:
            blogger.update_content(str(post["id"]), html)
            changed += 1
            print(f"     ✅ 본문 수정: {post.get('url', '')}")
    print(f"{'적용' if args.apply else '미리 보기'}: 이미지 넣을 글 {changed} · 건너뜀 {skipped}"
          + ("" if args.apply else " — 적용하려면 --apply"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
