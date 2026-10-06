"""PC와 무관한 Codex 작성기의 작업 선택·임대·재시도 정책."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .cloud_drafts import STATE, read_json
from .codex_fallback import parsed_time

STATE_FILE = "codex.json"
RETRY_WAIT = timedelta(hours=4)
MAX_ATTEMPTS = 2
LEASE_TIME = timedelta(hours=2)


def cloud_lease(tree: Path, now: datetime) -> dict:
    lease = read_json(tree / STATE / STATE_FILE).get("lease") or {}
    expires = parsed_time(lease.get("expires")) if isinstance(lease, dict) else None
    return lease if expires and expires > now and lease.get("owner") else {}


def task_key(task: dict) -> str:
    return f"{task['day']}:{task.get('request') or 'daily'}:{task.get('category') or 'auto'}"


def retry_ready(state: dict, task: dict, now: datetime) -> bool:
    attempt = (state.get("attempts") or {}).get(task_key(task)) or {}
    at = parsed_time(attempt.get("at"))
    count = int(attempt.get("count") or 0)
    if count >= MAX_ATTEMPTS:
        return False
    recovery = (state.get("retry_authorizations") or {}).get(task_key(task)) or {}
    expires = parsed_time(recovery.get("expires"))
    authorized = bool(attempt.get("status") == "failed" and recovery.get("reason")
                      and recovery.get("after_count") == count and expires and now < expires)
    return authorized or not at or now - at >= RETRY_WAIT


def choose_task(status: dict, state: dict, now: datetime) -> dict:
    """한 번에 한 편. 수동 지시 우선, 작성 중·아침 대기 중인 일일 원고는 기다린다."""
    if not status.get("synced") or status.get("locked_by"):
        return {}
    base = {"day": now.strftime("%Y%m%d"), "request": "", "kind": "daily", "category": "", "topic": ""}
    for request in status.get("requests") or []:
        if request.get("finish_only"):
            return {**base, **request, "request": request["id"], "finish_only": True}
        for category in request.get("missing") or []:
            task = {**base, **request, "request": request["id"], "category": category}
            if retry_ready(state, task, now):
                return task
    if status.get("cloud_running") or status.get("cloud_pending"):
        return {}
    for gap in status.get("daily") or []:
        task = {**base, **gap}
        if retry_ready(state, task, now):
            return task
    return {}


def reserve(state: dict, task: dict, owner: str, now: datetime) -> dict:
    attempts = {k: v for k, v in (state.get("attempts") or {}).items() if k.startswith(task["day"] + ":")}
    key = task_key(task)
    previous = attempts.get(key) or {}
    attempts[key] = {"at": now.isoformat(timespec="seconds"), "count": int(previous.get("count") or 0) + 1,
                     "status": "writing"}
    authorizations = dict(state.get("retry_authorizations") or {})
    authorizations.pop(key, None)  # 사용자가 요청한 복구 재시도는 한 번만 허용하며 횟수 상한은 유지한다.
    return {**state, "attempts": attempts, "retry_authorizations": authorizations,
            "lease": {"owner": owner, "at": now.isoformat(timespec="seconds"),
                      "expires": (now + LEASE_TIME).isoformat(timespec="seconds"), "task": task}}


def settle(state: dict, task: dict, owner: str, result: str, note: str = "") -> dict:
    if (state.get("lease") or {}).get("owner") != owner:
        raise ValueError("Codex 클라우드 임대 주인이 바뀌었습니다")
    attempts = dict(state.get("attempts") or {})
    key = task_key(task)
    attempts[key] = {**attempts.get(key, {}), "status": result, "note": note[:300]}
    return {**state, "lease": {}, "attempts": attempts}


def validate_verification(research: dict, verification: dict) -> None:
    """작성 단계와 전송 단계가 같은 검증 보고서 규칙을 검사한다."""
    claims = verification.get("claims")
    if not isinstance(claims, list) or not claims or not all(
        isinstance(c, dict) and isinstance(c.get("claim"), str) and c["claim"].strip()
        and isinstance(c.get("url"), str) and c["url"].strip() and isinstance(c.get("confirmed"), bool)
        and (c["confirmed"] or c.get("action")) for c in claims):
        raise ValueError("별도 사실 검증 근거가 비어 있거나 확인 불가한 주장에 조치가 없습니다")
    fc = research.get("factcheck") or {}
    if not isinstance(fc, dict) or type(fc.get("claims")) is not int or type(fc.get("confirmed")) is not int \
            or fc["claims"] != len(claims) or fc["confirmed"] != sum(c["confirmed"] for c in claims):
        raise ValueError("사실 검증 개수와 주장별 검증 기록이 맞지 않습니다")


def validate_primary_source(research: dict) -> None:
    """대표 원문의 날짜 근거를 출처 목록에서 확인할 수 있어야 한다."""
    source = research.get("source_url")
    sources = research.get("sources")
    if not isinstance(source, str) or not source.strip() or not isinstance(sources, list) or not any(
        isinstance(item, dict) and item.get("url") == source for item in sources):
        raise ValueError("대표 원문(source_url)이 sources 목록에 없습니다")
