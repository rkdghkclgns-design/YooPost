"""Claude가 멈춘 수동 지시의 Codex 인계 판단. AI 호출·발행·비밀값 접근 없음."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping

from .cloud_drafts import (
    DRAFTS, RUNS, SKIP_FILE, STATE, draft_folders, folder_category, is_complete,
    is_parked, is_skip, read_json, recent_days,
)
from .constants import CATEGORIES
from .draft_request import MAX_AGE_HOURS, request_id_of, request_path

PENDING_WAIT = timedelta(minutes=15)
RUNNING_WAIT = timedelta(hours=3)
QUOTA_ERROR = re.compile(
    r"429|한도|소진|quota|rate.?limit|usage.?limit|credit.?exhaust|out of credits|token.?limit", re.I,
)


def parsed_time(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def takeover_reason(record: Mapping, now: datetime) -> str:
    """실제 잔량 대신 한도 오류·미시작·오래 멈춘 실행을 판단한다. 종료된 요청은 재개하지 않는다."""
    if record.get("finished") or record.get("kind") not in ("single", "all"):
        return ""
    created = parsed_time(record.get("created"))
    fallback = record.get("fallback")
    first = parsed_time(fallback.get("first_taken_over")) if isinstance(fallback, Mapping) else None
    continuing = bool(first and created and fallback.get("mode") == "cloud"
                      and timedelta(0) <= first - created <= timedelta(hours=MAX_AGE_HOURS)
                      and timedelta(0) <= now - first <= timedelta(hours=24))
    if created is None or now < created or (now - created > timedelta(hours=MAX_AGE_HOURS) and not continuing):
        return ""
    if isinstance(fallback, Mapping) and fallback.get("mode") == "cloud" and fallback.get("status") == "waiting":
        return "Codex 클라우드에서 남은 카테고리 이어쓰기"
    taken_over = parsed_time(fallback.get("taken_over")) if isinstance(fallback, Mapping) else None
    started = parsed_time(record.get("started"))
    activity = max(t for t in (created, started, taken_over) if t is not None)
    if started or taken_over:
        return "작성 시작 후 3시간 이상 중단" if now - activity >= RUNNING_WAIT else ""
    error = str(record.get("fire_error") or "")
    if error:
        if "응답 없음" in error and now - created < PENDING_WAIT:
            return ""
        return "Claude 실행 한도 오류" if QUOTA_ERROR.search(error) else "Claude 실행 불가 또는 응답 유실 후 미시작"
    return "접수 후 15분 동안 작성 미시작" if now - created >= PENDING_WAIT else ""


def fallback_requests(tree: Path, now: datetime, pushed: set[str]) -> list[dict]:
    """올라간 원고는 다시 쓰지 않는다. 요청 주제는 명령이 아닌 데이터로 반환한다."""
    failures = read_json(tree / STATE / "failures.json")
    folders = draft_folders(tree / DRAFTS, recent_days(now))
    candidates = []
    for path in sorted((tree / RUNS / "requests").glob("*.json")):
        record = read_json(path)
        rid = str(record.get("id") or "")
        try:
            if request_path(tree, rid) != path:
                continue
        except ValueError:
            continue
        reason = takeover_reason(record, now)
        if not reason:
            continue
        category = str(record.get("category") or "")
        wanted = list(CATEGORIES) if record["kind"] == "all" else [category]
        completed = {
            folder_category(f.name) for f in folders
            if f.name in pushed and request_id_of(f) == rid
            and (is_complete(f) or is_skip(f)) and not is_parked(f, failures)
        }
        # 카테고리 미정 single도 원고 1편이 올라갔으면 끝난 것으로 본다.
        missing = [] if record["kind"] == "single" and completed else [c for c in wanted if c not in completed]
        candidates.append({"id": rid, "kind": record["kind"], "category": category,
                           "topic": str(record.get("topic") or ""), "reason": reason,
                           "missing": missing, "finish_only": not missing})
    return candidates


def takeover_record(record: Mapping, now: datetime, reason: str, category: str = "") -> dict:
    chosen = str(record.get("category") or category)
    if record["kind"] == "single" and chosen not in CATEGORIES:
        raise ValueError("주제에 맞는 카테고리를 --category 로 정해 주세요")
    if record["kind"] == "single" and category and record.get("category") and category != record["category"]:
        raise ValueError("요청에 정해진 카테고리를 바꿀 수 없습니다")
    updated = {**record, "category": chosen, "started": record.get("started") or now.isoformat(timespec="seconds"),
               "network": "full", "fallback": {"provider": "codex", "taken_over": now.isoformat(timespec="seconds"),
                                                "reason": reason}}
    if updated.get("fire_error"):
        updated["claude_fire_error"] = updated.pop("fire_error")
    return updated
