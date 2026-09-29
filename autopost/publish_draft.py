"""슬랙 ✅ 승인 → 원고 발행 (GitHub Actions 'publish_draft.yml' 이 실행).

Worker 가 ✅ 반응을 감지해 원고 ID 로 이 워크플로를 실행한다. 여기서는 AI 를 쓰지 않는다:
원고 보관함에서 발행용 HTML 을 꺼내 구글 블로그에 올리고, 결과를 슬랙 원고 메시지의 스레드에 남긴다.
이미 발행한 원고는 다시 올리지 않는다 (✅ 를 여러 번 눌러도 한 번만).

    python -m autopost.publish_draft --id 20260929_1300_1 --approved-by U0123
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .blog_publisher import PLATFORM, BloggerPublisher
from .config import ConfigError, load_config
from .draft_store import DraftStore
from .results import PublishResult
from .slack_helper import LABEL, SlackNotifier, escape_mrkdwn

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def _message(draft: dict, result: PublishResult, approved_by: str) -> str:
    who = f" · 승인 <@{approved_by}>" if approved_by.isalnum() else ""
    title = escape_mrkdwn(str(draft.get("title") or ""))
    if result.ok:
        return f"✅ {LABEL} 발행 완료 — <{result.url}|{title}>{who}"
    return f"❌ {LABEL} 발행 실패 — {title}\n{escape_mrkdwn(result.message[:300])}\n✅ 를 다시 누르면 다시 시도합니다"


def publish_draft(draft_id: str, store: DraftStore, blogger: BloggerPublisher, slack: SlackNotifier,
                  approved_by: str = "", publish_channel: str = "",
                  now=lambda: datetime.now(KST)) -> int:
    draft = store.get(draft_id)
    place = draft.get("slack") if isinstance(draft.get("slack"), dict) else {}
    if draft.get("status") == "published":
        log.info("이미 발행한 원고입니다: %s", draft.get("url"))
        slack.post(f"ℹ️ 이미 발행한 원고입니다 — {draft.get('url', '')}", thread_ts=place.get("ts"),
                   channel=place.get("channel"))
        return 0
    try:
        result = blogger.publish(str(draft["title"]), str(draft["html"]), list(draft.get("labels") or []))
    except Exception as exc:
        log.exception("구글 블로그 발행 실패")
        result = PublishResult.fail(PLATFORM, exc)
    store.put(draft_id, {**draft, "status": "published" if result.ok else "failed", "url": result.url,
                         "approved_by": approved_by, "published_at": now().isoformat(timespec="seconds")})
    text = _message(draft, result, approved_by)
    slack.post(text, thread_ts=place.get("ts"), channel=place.get("channel"))
    if result.ok and publish_channel:
        slack.post(text, channel=publish_channel)
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        stream=sys.stdout)
    parser = argparse.ArgumentParser(description="슬랙 ✅ 승인된 원고 발행")
    parser.add_argument("--id", default=os.environ.get("DRAFT_ID", ""))
    parser.add_argument("--approved-by", default=os.environ.get("APPROVED_BY", ""))
    args = parser.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    missing = [name for name, ok in (("원고 ID", args.id), ("GOOGLE_*", cfg.google), ("BLOGGER_BLOG_ID", cfg.blogger_blog_id),
                                     ("IMAGE_HOST_URL·IMAGE_UPLOAD_TOKEN", cfg.image_host_enabled)) if not ok]
    slack = SlackNotifier(cfg.slack_bot_token, cfg.slack_channel_id, cfg.slack_webhook_url, secrets=cfg.secret_values())
    if missing:
        log.error("설정 누락: %s", ", ".join(missing))
        slack.notify_error("원고 발행", "설정 누락: " + ", ".join(missing))
        return 2
    from .google_auth import build_blogger_service, build_credentials

    blogger = BloggerPublisher(build_blogger_service(build_credentials(cfg.google)), cfg.blogger_blog_id)
    store = DraftStore(cfg.image_host_url, cfg.image_upload_token)
    publish_channel = dict(cfg.slack_stage_channels).get("publish", "")
    try:
        return publish_draft(args.id, store, blogger, slack, args.approved_by, publish_channel)
    except Exception as exc:
        log.exception("원고 발행 중 오류")
        slack.notify_error("원고 발행", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
