"""슬랙 ✅ 승인 → 원고 발행 (GitHub Actions 'publish_draft.yml' 이 실행).

Worker 가 ✅ 반응을 감지해 원고 ID 로 이 워크플로를 실행한다. 여기서는 AI 를 쓰지 않는다:
원고 보관함에서 발행용 HTML 을 꺼내 구글 블로그에 올리고, 결과를 슬랙 원고 메시지의 스레드에 남긴다.
이미 발행한 원고는 다시 올리지 않는다 (✅ 를 여러 번 눌러도 한 번만).
발행 직전에 블로그 글 전부와 다시 비교해 겹치면 ✅ 를 눌렀어도 발행하지 않고 제외한다 (2026-10-01 사용자 지시).

    python -m autopost.publish_draft --id 20260929_1300_1 --approved-by U0123
"""
from __future__ import annotations

import argparse
import logging
import json
import re
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .blog_publisher import PLATFORM, BloggerPublisher
from .config import ConfigError, load_config
from .draft_request import manual_thread
from .draft_store import DraftStore
from .exclusion import EXCLUDED, exclude, overlap_of, record_entry
from .history import HistoryUnavailable, load_history, read_marker
from .results import PublishResult
from .slack_helper import AUTO_APPROVAL_LABEL, LABEL, SlackNotifier, escape_mrkdwn

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def publication_html(draft_id: str, draft: dict) -> str:
    html = str(draft["html"])
    marker = {**read_marker(html), "draft_id": draft_id}
    marker.setdefault("topic", str(draft.get("topic") or draft.get("title") or ""))
    marker.setdefault("source", str(draft.get("source_url") or ""))
    marker.setdefault("category", str(draft.get("category") or ""))
    tag = "<!--yoopost " + json.dumps(marker, ensure_ascii=True, separators=(",", ":")) + "-->"
    pattern = r"<!--yoopost (\{.*?\})-->"
    return re.sub(pattern, lambda _: tag, html, count=1, flags=re.DOTALL) if re.search(pattern, html, re.DOTALL) else html + "\n" + tag


def _message(draft: dict, result: PublishResult, approved_by: str) -> str:
    who = f" · {AUTO_APPROVAL_LABEL}" if approved_by == "auto-60s" else (
        f" · 승인 <@{approved_by}>" if approved_by.isalnum() else "")
    title = escape_mrkdwn(str(draft.get("title") or ""))
    if result.ok:
        return f"✅ {LABEL} 발행 완료 — <{result.url}|{title}>{who}"
    return f"❌ {LABEL} 발행 실패 — {title}{who}\n{escape_mrkdwn(result.message[:300])}\n✅ 를 다시 누르면 다시 시도합니다"


def publish_draft(draft_id: str, store: DraftStore, blogger: BloggerPublisher, slack: SlackNotifier,
                  approved_by: str = "", publish_channel: str = "",
                  now=lambda: datetime.now(KST), manual_channel: str = "") -> int:
    draft = store.get(draft_id)
    place = draft.get("slack") if isinstance(draft.get("slack"), dict) else {}
    if draft.get("status") == "published":
        log.info("이미 발행한 원고입니다: %s", draft.get("url"))
        slack.post(f"ℹ️ 이미 발행한 원고입니다 — {draft.get('url', '')}", thread_ts=place.get("ts"),
                   channel=place.get("channel"))
        return 0
    if draft.get("status") == EXCLUDED:
        slack.post(f"🚫 겹치는 원고라 제외된 원고입니다 — 발행하지 않습니다 ({escape_mrkdwn(str(draft.get('excluded_reason') or ''))})",
                   thread_ts=place.get("ts"), channel=place.get("channel"))
        return 0
    if draft.get("status") not in ("approved", "failed"):
        log.warning("승인되지 않았거나 보류된 원고는 발행하지 않습니다: %s", draft_id)
        return 1
    approved_by = str(draft.get("approved_by") or approved_by)
    def notify_result(result: PublishResult) -> None:
        text = _message(draft, result, approved_by)
        slack.post(text, thread_ts=place.get("ts"), channel=place.get("channel"))
        if result.ok and publish_channel:
            slack.post(text, channel=publish_channel)
        request = draft.get("request") if isinstance(draft.get("request"), dict) else {}
        place_manual = manual_thread(str(request.get("id") or ""), manual_channel)
        if place_manual:
            slack.post(text, thread_ts=place_manual[1], channel=place_manual[0])
    try:  # 발행 직전 겹침 확인 — 블로그 목록을 못 읽으면 확인 없이 올리지 않는다
        history = load_history(blogger, strict=True)
    except HistoryUnavailable as exc:
        store.put(draft_id, {**draft, "status": "failed"})
        slack.post(f"❌ {LABEL} 발행 전 겹침 확인을 하지 못해 발행하지 않았습니다 — {escape_mrkdwn(str(exc)[:200])}\n"
                   "✅ 를 다시 누르면 다시 시도합니다", thread_ts=place.get("ts"), channel=place.get("channel"))
        return 1
    # insert 응답 유실·runner 종료·상태 저장 실패: 새 글을 만들기 전에 이미 발행된 같은 ID를 찾는다.
    own = next((e for e in history.entries if e.draft_id == draft_id), None)
    if own:
        result = PublishResult(PLATFORM, True, own.urls.get("blogger", ""), "기존 발행 복구")
        store.put(draft_id, {**draft, "status": "published", "url": result.url, "approved_by": approved_by,
                             "published_at": now().isoformat(timespec="seconds"), "publish_unknown": False})
        notify_result(result)
        return 0
    found = overlap_of(record_entry(draft_id, draft), history)
    if found:
        exclude(draft_id, draft, store, slack, found, now(), manual_channel)
        return 0
    # 원격 쓰기 전에 상태를 남긴다. 이 저장이 실패하면 Blogger insert는 수행하지 않는다.
    draft = {**draft, "approved_by": approved_by, "publish_started_at": now().isoformat(timespec="seconds")}
    store.put(draft_id, draft)
    try:
        result = blogger.publish(str(draft["title"]), publication_html(draft_id, draft), list(draft.get("labels") or []))
    except Exception as exc:
        log.exception("구글 블로그 발행 실패")
        result = PublishResult.fail(PLATFORM, exc)
    store.put(draft_id, {**draft, "status": "published" if result.ok else "failed", "url": result.url,
                         "approved_by": approved_by, "publish_unknown": not result.ok,
                         "published_at": now().isoformat(timespec="seconds") if result.ok else ""})
    notify_result(result)
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
        return publish_draft(args.id, store, blogger, slack, args.approved_by, publish_channel,
                             manual_channel=dict(cfg.slack_stage_channels).get("manual", ""))
    except Exception as exc:
        log.exception("원고 발행 중 오류")
        slack.notify_error("원고 발행", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
