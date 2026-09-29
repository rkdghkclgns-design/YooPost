"""이미 발행한 글·승인 대기 원고·대표 이미지를 새 디자인(Hashnode 스타일)으로 바꾼다. 내용은 그대로.

    python scripts/restyle_posts.py            # 미리 보기 — 바뀔 글과 바뀌는 곳 수만 출력 (아무것도 바꾸지 않음)
    python scripts/restyle_posts.py --apply    # 적용 — 발행 글 본문 수정 · 원고 보관함 갱신 · 새 대표 이미지로 교체

이미지 저장소는 1년 캐시(immutable)라 같은 주소에 덮어쓰면 바뀌지 않는다 → 새 이름(<이름>_v2.png)으로 올리고 글의 주소를 바꾼다.

AI 호출 없음. 필요(.env): GOOGLE_*·BLOGGER_BLOG_ID, IMAGE_HOST_URL·IMAGE_UPLOAD_TOKEN.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from autopost.blog_publisher import BloggerPublisher  # noqa: E402
from autopost.cli import http_session  # noqa: E402
from autopost.config import load_config  # noqa: E402
from autopost.constants import CATEGORIES  # noqa: E402
from autopost.cover import make_cover  # noqa: E402
from autopost.draft_store import DraftStore  # noqa: E402
from autopost.google_auth import build_blogger_service, build_credentials  # noqa: E402
from autopost.image_host import ImageHost  # noqa: E402
from autopost.restyle import find_cover, marker_category, restyle_html, versioned_key  # noqa: E402

DRAFTS = ROOT / "output" / "claude_drafts"
RECENT_DAYS = 14
EDITABLE = ("pending", "failed")  # 승인 대기·발행 실패 원고만 (발행 중인 원고는 건드리지 않음)


def recent_draft_ids() -> list[str]:
    cutoff = time.time() - RECENT_DAYS * 86400
    return sorted(p.name for p in DRAFTS.glob("*_*_*") if p.is_dir() and p.stat().st_mtime >= cutoff)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="발행 글·원고·대표 이미지를 새 디자인으로")
    parser.add_argument("--apply", action="store_true", help="실제로 바꾼다 (없으면 미리 보기)")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    cfg = load_config()
    if cfg.google is None or not cfg.blogger_blog_id or not cfg.image_host_enabled:
        print("❌ .env 에 GOOGLE_*·BLOGGER_BLOG_ID·IMAGE_HOST_URL·IMAGE_UPLOAD_TOKEN 이 필요합니다")
        return 2
    session = http_session()
    blogger = BloggerPublisher(build_blogger_service(build_credentials(cfg.google)), cfg.blogger_blog_id)
    store = DraftStore(cfg.image_host_url, cfg.image_upload_token, session)
    host = ImageHost(cfg.image_host_url, cfg.image_upload_token, session)
    mode = "적용" if args.apply else "미리 보기"
    uploaded: dict[str, str] = {}  # 예전 대표 이미지 주소 → 새 주소
    tmp = Path(tempfile.mkdtemp())

    def redesign(html: str, category: str) -> tuple[str, int]:
        result = restyle_html(html)
        html, changes = result.html, result.changes
        cover = find_cover(html, cfg.image_host_url)
        if cover:
            changes += 1
            print(f"     🖼️ 대표 이미지 → {versioned_key(cover.key)} ({category})")
            if args.apply:
                if cover.url not in uploaded:
                    path = make_cover(tmp / versioned_key(cover.key), cover.title, category, cfg.blog_name,
                                      cover.date_text)
                    uploaded[cover.url] = host.upload(path, versioned_key(cover.key))
                html = html.replace(cover.url, uploaded[cover.url])
        return html, changes

    print(f"== 발행 글 ({mode})")
    for post in blogger.editable_posts():
        content = str(post.get("content") or "")
        category = marker_category(content, list(post.get("labels") or []), CATEGORIES)
        print(f"  · {post.get('title', '')[:60]}")
        html, changes = redesign(content, category)
        print(f"     바뀌는 곳 {changes}")
        if args.apply and changes and html != content:
            blogger.update_content(str(post["id"]), html)

    print(f"== 승인 대기 원고 ({mode})")
    for draft_id in recent_draft_ids():
        record = store.find(draft_id)
        if not record or str(record.get("status") or "pending") not in EDITABLE:
            continue
        content = str(record.get("html") or "")
        print(f"  · {draft_id} {str(record.get('title', ''))[:50]}")
        html, changes = redesign(content, str(record.get("category") or CATEGORIES[0]))
        print(f"     바뀌는 곳 {changes}")
        if args.apply and changes and html != content:
            store.put(draft_id, {**record, "html": html})
    print("✅ 끝" if args.apply else "미리 보기만 했습니다 — 적용하려면 --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
