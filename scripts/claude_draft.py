"""Claude 가 쓴 원고 폴더 → 발행 준비 → 슬랙 승인 요청 (✅ 를 누르면 발행).

    python scripts/claude_draft.py output/claude_drafts/<ID>              # 준비 + 슬랙 전송
    python scripts/claude_draft.py output/claude_drafts/<ID> --no-send    # 준비·미리보기만 (슬랙·보관함 안 씀)
    python scripts/claude_draft.py output/claude_drafts/<ID> --allow-duplicate
    python scripts/claude_draft.py output/claude_drafts/<ID> --any-date    # 사용자가 대화로 정한 상시 주제 (최신 이슈 조건 끔)

매일 원고는 최신 이슈만 (대표 원문 발표일·여러 매체 보도 확인) — 이미 쓴 글·원고 브랜치의 다른 원고와 겹치면 멈춘다.

구글 AI API 를 쓰지 않는다. 필요한 설정(.env): GOOGLE_*·BLOGGER_BLOG_ID(발행 기록 읽기), IMAGE_HOST_URL·IMAGE_UPLOAD_TOKEN,
SLACK_BOT_TOKEN·SLACK_CHANNEL_ID (원고 채널은 SLACK_CHANNEL_DRAFT 가 있으면 그쪽).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from autopost.blog_publisher import BloggerPublisher  # noqa: E402
from autopost.claude_draft import DraftError, DraftPreparer, send_for_approval  # noqa: E402
from autopost.cli import http_session  # noqa: E402
from autopost.cloud_drafts import (  # noqa: E402
    DRAFTS, STATE, WORKTREE, pending_entries, pushed_names, read_json, user_topic,
)
from autopost.config import load_config  # noqa: E402
from autopost.draft_store import DraftStore  # noqa: E402
from autopost.google_auth import build_blogger_service, build_credentials  # noqa: E402
from autopost.history import HistoryUnavailable  # noqa: E402
from autopost.image_host import ImageHost  # noqa: E402
from autopost.pipeline import KST  # noqa: E402
from autopost.quote_images import ImageCollector  # noqa: E402
from autopost.slack_helper import SlackNotifier  # noqa: E402


TREE = ROOT / WORKTREE


def earlier_drafts(folder: Path) -> tuple:
    """원고 브랜치에 올라간 다른 원고 — ✅ 전이라 블로그에 없어도 같은 소식을 또 보내지 않도록."""
    if not (TREE / DRAFTS).is_dir():
        return ()
    return pending_entries(TREE / DRAFTS, datetime.now(KST), read_json(TREE / STATE / "failures.json"),
                           exclude=folder.name, include=pushed_names(TREE))


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude 원고 → 슬랙 승인 요청")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--no-send", action="store_true", help="준비·미리보기만")
    parser.add_argument("--allow-duplicate", action="store_true", help="이미 쓴 주제와 겹쳐도 진행")
    parser.add_argument("--any-date", action="store_true", help="최신 이슈 조건을 끔 (사용자가 직접 정한 상시 주제)")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config()
    if cfg.google is None or not cfg.blogger_blog_id or not cfg.image_host_enabled:
        print("❌ .env 에 GOOGLE_*·BLOGGER_BLOG_ID·IMAGE_HOST_URL·IMAGE_UPLOAD_TOKEN 이 필요합니다")
        return 2
    session = http_session()
    blogger = BloggerPublisher(build_blogger_service(build_credentials(cfg.google)), cfg.blogger_blog_id)
    host = None if args.no_send else ImageHost(cfg.image_host_url, cfg.image_upload_token, session)
    preparer = DraftPreparer(cfg.blog_name, blogger, host, ImageCollector(session), session=session,
                             now=lambda: datetime.now(KST))
    folder = args.folder.resolve()
    try:
        prepared = preparer.prepare(folder, folder.name, allow_duplicate=args.allow_duplicate,
                                    pending=earlier_drafts(folder),
                                    fresh_required=not args.any_date and not user_topic(TREE, folder))
    except (DraftError, HistoryUnavailable) as exc:
        print(f"❌ {exc}")
        return 1
    print(f"✅ 준비 완료: {prepared.content.title}")
    print(f"   카테고리 {prepared.content.category} · 본문 {prepared.content.text_length():,}자 · {prepared.fact_line}")
    for warning in prepared.warnings:
        print(f"   ⚠️ {warning}")
    print(f"   미리보기: {prepared.preview}")
    if args.no_send:
        return 0
    channel = dict(cfg.slack_stage_channels).get("draft") or cfg.slack_channel_id
    slack = SlackNotifier(cfg.slack_bot_token, cfg.slack_channel_id, cfg.slack_webhook_url,
                          secrets=cfg.secret_values())
    store = DraftStore(cfg.image_host_url, cfg.image_upload_token, session)
    try:
        ts = send_for_approval(prepared, store, slack, channel, datetime.now(KST))
    except Exception as exc:
        print(f"❌ 슬랙 승인 요청 실패: {exc}")
        return 1
    print(f"📨 슬랙에 승인 요청을 보냈습니다 (채널 {channel}, 메시지 {ts}) — ✅ 를 누르면 발행됩니다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
