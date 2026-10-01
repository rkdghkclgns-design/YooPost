"""겹치는 원고 자동 제외 (2026-10-01 사용자 지시) — 이미 발행한 글·먼저 보낸 원고와 겹치면 ✅ 를 눌러도 발행하지 않는다.

두 곳에서 막는다.
1. Actions 실행마다(sweep): 승인 대기 원고를 발행한 글·먼저 보낸 원고와 다시 비교해, 겹치면 보관함 상태를 'excluded' 로
   바꾸고 슬랙 원고 카드를 '🚫 제외됨'으로 고친다. Worker 는 pending·failed 원고만 발행 요청을 보내므로 ✅ 를 눌러도 발행되지 않는다.
   Actions 멈춤 기록(failures.json)에도 남겨 그 카테고리를 빈칸으로 치게 한다 (다른 소식으로 다시 쓸 수 있게).
2. 발행 직전(publish_draft): 승인 뒤 다른 글이 먼저 발행됐을 수도 있으므로 블로그 글 전부와 한 번 더 비교한다.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .cloud_drafts import (
    DRAFTS, STATE, delivered, draft_entry, draft_folders, fingerprint, is_skip, read_json, recent_days, write_json,
)
from .draft_request import manual_thread
from .history import HistoryEntry, PostHistory, combined, read_marker
from .slack_helper import LABEL, escape_mrkdwn

log = logging.getLogger(__name__)

EXCLUDED = "excluded"
SWEEP_DAYS = 3  # 승인 대기 카드를 다시 비교하는 기간 (그보다 오래된 카드는 발행 직전 확인이 막는다)
OVERLAP_PREFIX = "이미 쓴 글과 겹칩니다"
STALE_PREFIX = "최신 이슈가 아닙니다"


def record_entry(draft_id: str, draft: Mapping[str, object]) -> HistoryEntry:
    """보관함 원고 기록 → 겹침 비교용 기록 (예전 원고는 발행용 HTML 의 표시 주석에서 출처를 읽는다)."""
    marker = read_marker(str(draft.get("html") or ""))
    sources = draft.get("sources") or marker.get("sources") or ()
    return HistoryEntry(
        date="", title=str(draft.get("title") or ""),
        topic=str(draft.get("topic") or marker.get("topic") or ""),
        category=str(draft.get("category") or marker.get("category") or ""),
        source_url=str(draft.get("source_url") or marker.get("source") or ""),
        sources=tuple(str(u) for u in sources if isinstance(u, str) and u) if isinstance(sources, (list, tuple)) else (),
        where=f"먼저 보낸 원고 {draft_id}",
    )


def overlap_of(entry: HistoryEntry, history: PostHistory) -> tuple[HistoryEntry, str] | None:
    return history.find_duplicate(entry.topic, entry.source_url, entry.title, entry.sources)


def _describe(found: tuple[HistoryEntry, str]) -> str:
    entry, reason = found
    link = entry.urls.get("blogger", "")
    return f"{entry.where or '발행한 글'} '{entry.title}' ({reason}){' ' + link if link else ''}"


def exclude(draft_id: str, draft: Mapping[str, object], store, slack, found: tuple[HistoryEntry, str],
            now: datetime, manual_channel: str = "") -> dict:
    """원고를 제외한다 — 보관함 상태를 먼저 바꾸고(✅ 로 발행되지 않게), 슬랙 카드·스레드에 알린다."""
    note = _describe(found)
    updated = {**draft, "status": EXCLUDED, "excluded_reason": note, "excluded_at": now.isoformat(timespec="seconds")}
    store.put(draft_id, updated)
    title = escape_mrkdwn(str(draft.get("title") or draft_id))
    text = (f"🚫 {LABEL} 겹치는 원고라 자동으로 제외했습니다 — ✅ 를 눌러도 발행하지 않습니다\n"
            f"• 원고: {title}\n• 겹친 글: {escape_mrkdwn(note)}")
    place = draft.get("slack") if isinstance(draft.get("slack"), Mapping) else {}
    ts, channel = str(place.get("ts") or ""), str(place.get("channel") or "") or None
    if ts:
        slack.update(ts, text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}], channel=channel)
        slack.post(text, thread_ts=ts, channel=channel)
    request = draft.get("request") if isinstance(draft.get("request"), Mapping) else {}
    thread = manual_thread(str(request.get("id") or ""), manual_channel)
    if thread:  # 수동 지시 원고면 지시 스레드에도
        slack.post(text, thread_ts=thread[1], channel=thread[0])
    return updated


def _with_branch_sources(entry: HistoryEntry, folder: Path) -> HistoryEntry:
    """예전 보관함 기록에는 출처 목록이 없다 — 원고 브랜치의 research.json 출처로 채운다."""
    if entry.sources:
        return entry
    branch = draft_entry(folder)
    if branch is None:
        return entry
    return replace(entry, sources=branch.sources, topic=entry.topic or branch.topic,
                   source_url=entry.source_url or branch.source_url)


def _park(records: dict, folder: Path, note: str) -> None:
    """Actions 멈춤 기록 — 이 원고는 쓸 만한 원고가 아니다 (빈 카테고리로 쳐서 다른 소식으로 다시 쓰게)."""
    records[folder.name] = {"hash": fingerprint(folder), "reason": f"겹침으로 제외: {note}"[:300], "attempts": 1,
                            "permanent": True, "excluded": True}


def sweep(root: Path, now: datetime, store, history: PostHistory, slack, manual_channel: str = "") -> list[str]:
    """승인 대기 원고를 발행한 글·먼저 보낸 원고(이름순)와 다시 비교해 겹치면 제외한다. 제외한 원고 ID 목록."""
    failures_file = root / STATE / "failures.json"
    records = dict(read_json(failures_file))
    accepted: list[HistoryEntry] = []
    excluded: list[str] = []
    for folder in draft_folders(root / DRAFTS, recent_days(now, SWEEP_DAYS)):
        if is_skip(folder):
            continue
        try:
            draft = store.find(folder.name)
        except Exception as exc:  # 보관함을 못 읽은 원고는 이번에 건너뛴다 (발행 직전 확인이 남아 있다)
            log.warning("보관함 확인 실패 %s: %s", folder.name, exc)
            continue
        if not draft or not delivered(draft):
            continue
        status = str(draft.get("status") or "pending")
        if status == EXCLUDED:
            if not records.get(folder.name, {}).get("excluded"):
                _park(records, folder, str(draft.get("excluded_reason") or ""))
            continue
        entry = _with_branch_sources(record_entry(folder.name, draft), folder)
        if status in ("pending", "failed", "approved"):  # approved = ✅ 했지만 아직 발행 전 (발행 단계도 같은 확인을 한다)
            found = overlap_of(entry, combined(history, accepted))
            if found:
                updated = exclude(folder.name, draft, store, slack, found, now, manual_channel)
                _park(records, folder, str(updated["excluded_reason"]))
                excluded.append(folder.name)
                continue
        accepted.append(entry)  # 발행했거나 발행 중이거나 살아남은 원고 — 뒤 원고는 이 원고와 겹치면 제외
    write_json(failures_file, records)
    return excluded


def failure_text(draft_id: str, reason: str) -> str:
    """Actions 가 보내기 전에 거절한 원고 알림 — 겹침·오래된 소식은 '자동 제외'로 알린다."""
    if reason.startswith(OVERLAP_PREFIX):
        return f"🚫 겹치는 원고라 자동으로 제외했습니다 (승인 요청을 보내지 않음) — `{draft_id}`: {escape_mrkdwn(reason)}"
    if reason.startswith(STALE_PREFIX):
        return f"🚫 최신 이슈가 아니라 자동으로 제외했습니다 (승인 요청을 보내지 않음) — `{draft_id}`: {escape_mrkdwn(reason)}"
    return f"⚠️ 클라우드 원고를 슬랙 승인 요청으로 보내지 못했습니다 — `{draft_id}`: {escape_mrkdwn(reason)}"
