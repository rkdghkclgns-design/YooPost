"""(GitHub Actions) 클라우드 루틴이 yoopost-drafts 브랜치에 올린 원고 → 슬랙 승인 요청 · 진행 막대 · 발행 기록.

    python scripts/cloud_sync.py --root .yoopost-drafts
    python scripts/cloud_sync.py --root .yoopost-drafts --watchdog   # 오늘 실행 기록이 없으면 #yoopost-알림 에 경고

.github/workflows/cloud_drafts.yml 이 원고 브랜치 push 마다 실행한다. 이미 보낸 원고(원고 보관함에 있음)는 다시 보내지 않는다.
필요: GOOGLE_*·BLOGGER_BLOG_ID, IMAGE_HOST_URL·IMAGE_UPLOAD_TOKEN, SLACK_BOT_TOKEN·SLACK_CHANNEL_ID (원고 채널 SLACK_CHANNEL_DRAFT).
AI 호출 없음.
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
from autopost.claude_draft import DraftPreparer, send_for_approval  # noqa: E402
from autopost.cli import http_session  # noqa: E402
from autopost.cloud_drafts import WORKTREE, sync_and_report, watchdog  # noqa: E402
from autopost.config import load_config  # noqa: E402
from autopost.draft_request import request_of  # noqa: E402
from autopost.draft_store import DraftStore  # noqa: E402
from autopost.google_auth import build_blogger_service, build_credentials  # noqa: E402
from autopost.history import load_history  # noqa: E402
from autopost.image_host import ImageHost  # noqa: E402
from autopost.pipeline import KST  # noqa: E402
from autopost.quote_images import ImageCollector  # noqa: E402
from autopost.slack_helper import SlackNotifier, redact  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="클라우드 원고 → 슬랙 승인 요청")
    parser.add_argument("--root", type=Path, default=ROOT / WORKTREE, help="yoopost-drafts 브랜치를 받은 폴더")
    parser.add_argument("--watchdog", action="store_true", help="오늘 클라우드 실행 기록이 있는지도 확인")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config()
    if cfg.google is None or not cfg.blogger_blog_id or not cfg.image_host_enabled:
        print("❌ GOOGLE_*·BLOGGER_BLOG_ID·IMAGE_HOST_URL·IMAGE_UPLOAD_TOKEN 이 필요합니다")
        return 2
    now = lambda: datetime.now(KST)  # noqa: E731
    session = http_session()
    blogger = BloggerPublisher(build_blogger_service(build_credentials(cfg.google)), cfg.blogger_blog_id)
    store = DraftStore(cfg.image_host_url, cfg.image_upload_token, session)
    slack = SlackNotifier(cfg.slack_bot_token, cfg.slack_channel_id, cfg.slack_webhook_url, secrets=cfg.secret_values())
    channel = dict(cfg.slack_stage_channels).get("draft") or cfg.slack_channel_id
    preparer = DraftPreparer(cfg.blog_name, blogger, ImageHost(cfg.image_host_url, cfg.image_upload_token, session),
                             ImageCollector(session), session=session, now=now)

    def send(folder: Path) -> str:
        prepared = preparer.prepare(folder, folder.name)
        ts = send_for_approval(prepared, store, slack, channel, now(), request=request_of(folder))
        print(f"📨 {folder.name} — {prepared.content.title}")
        for warning in prepared.warnings:
            print(f"   ⚠️ {warning}")
        return ts

    root = args.root.resolve()
    outcome = sync_and_report(root, now, store, send, slack, lambda: load_history(blogger).entries,
                              clean=lambda text: redact(text, cfg.secret_values()),
                              manual_channel=dict(cfg.slack_stage_channels).get("manual", ""), draft_channel=channel)
    if args.watchdog and not watchdog(root, now(), slack):
        print("⚠️ 오늘 클라우드 실행 기록이 없습니다")
    print(f"보냄 {len(outcome.sent)} · 이미 보냄 {len(outcome.already)} · 실패 {len(outcome.new_failures)}"
          f" · 확인 못 함 {len(outcome.unknown)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
