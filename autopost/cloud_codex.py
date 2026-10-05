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
    return int(attempt.get("count") or 0) < MAX_ATTEMPTS and (not at or now - at >= RETRY_WAIT)


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
    return {**state, "attempts": attempts,
            "lease": {"owner": owner, "at": now.isoformat(timespec="seconds"),
                      "expires": (now + LEASE_TIME).isoformat(timespec="seconds"), "task": task}}


def settle(state: dict, task: dict, owner: str, result: str, note: str = "") -> dict:
    if (state.get("lease") or {}).get("owner") != owner:
        raise ValueError("Codex 클라우드 임대 주인이 바뀌었습니다")
    attempts = dict(state.get("attempts") or {})
    key = task_key(task)
    attempts[key] = {**attempts.get(key, {}), "status": result, "note": note[:300]}
    return {**state, "lease": {}, "attempts": attempts}
