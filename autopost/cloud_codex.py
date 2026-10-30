"""PC와 무관한 Codex 작성기의 작업 선택·임대·재시도 정책."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .cloud_drafts import DRAFTS, SKIP_FILE, STATE, draft_folders, folder_category, is_skip, read_json
from .constants import CATEGORIES
from .draft_request import request_id_of
from .codex_fallback import parsed_time

STATE_FILE = "codex.json"
RETRY_WAIT = timedelta(hours=4)
MAX_ATTEMPTS = 2
LEASE_TIME = timedelta(hours=2)
DAILY_RETRY_MINUTES = (30, 60, 120)
SERVICE_FAILURES = {"authentication", "usage_limit", "cli_arguments", "sandbox", "unavailable"}


def daily_task(task: dict) -> bool:
    return not task.get("request") and task.get("kind") == "daily"


def retry_at(attempt: dict, task: dict) -> datetime | None:
    """완료 시각부터 기다린다. 일일 누락은 횟수로 중단하지 않는다."""
    at = parsed_time(attempt.get("finished_at") or attempt.get("at"))
    if not at or attempt.get("status") == "infrastructure_wait":
        return None
    wait = RETRY_WAIT
    if daily_task(task) and attempt.get("failure_kind") not in SERVICE_FAILURES:
        index = min(max(int(attempt.get("count") or 1) - 1, 0), len(DAILY_RETRY_MINUTES) - 1)
        wait = timedelta(minutes=DAILY_RETRY_MINUTES[index])
    return at + wait


def cloud_lease(tree: Path, now: datetime) -> dict:
    lease = read_json(tree / STATE / STATE_FILE).get("lease") or {}
    expires = parsed_time(lease.get("expires")) if isinstance(lease, dict) else None
    return lease if expires and expires > now and lease.get("owner") else {}


def task_key(task: dict) -> str:
    return f"{task['day']}:{task.get('request') or 'daily'}:{task.get('category') or 'auto'}"


def daily_skip_history(tree: Path, now: datetime, pushed: set[str]) -> dict:
    """첫 Codex 인계도 원격 일일 건너뜀의 완료 시각·횟수에서 이어 간다."""
    grouped: dict[str, list[tuple[datetime, str, dict]]] = {}
    day = now.strftime("%Y%m%d")
    for folder in draft_folders(tree / DRAFTS, [day]):
        if folder.name not in pushed or not is_skip(folder) or request_id_of(folder):
            continue
        skipped = read_json(folder / SKIP_FILE)
        category = folder_category(folder.name)
        at = parsed_time(skipped.get("at"))
        if category not in CATEGORIES or skipped.get("category") != category or not at \
                or at.astimezone(now.tzinfo).strftime("%Y%m%d") != day or at > now:
            continue
        grouped.setdefault(category, []).append((at, folder.name, skipped))
    history = {}
    for category, items in grouped.items():
        items.sort(key=lambda item: (item[0], item[1]))
        finished, _, last = items[-1]
        history[category] = {"at": finished.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"), "status": "skipped",
            "count": len(items), "note": str(last.get("reason") or "")[:300],
            "skip_ids": [item[1] for item in items]}
    return history


def task_attempt(state: dict, task: dict) -> dict:
    """기존 Codex 기록이 있으면 이력을 다시 합산하거나 완료 시각을 되돌리지 않는다."""
    attempts = state.get("attempts") or {}
    key = task_key(task)
    if key in attempts:
        return attempts[key] or {}
    return (task.get("previous_attempt") or {}) if daily_task(task) else {}


def retry_ready(state: dict, task: dict, now: datetime) -> bool:
    attempt = task_attempt(state, task)
    count = int(attempt.get("count") or 0)
    if daily_task(task) and task["day"] != now.strftime("%Y%m%d"):
        return False
    if not daily_task(task) and count >= MAX_ATTEMPTS:
        return False
    if attempt.get("status") == "infrastructure_wait":
        return True  # 실행 서버도 받지 못한 취소는 원고 작성 실패로 계산하지 않는다.
    recovery = (state.get("retry_authorizations") or {}).get(task_key(task)) or {}
    expires = parsed_time(recovery.get("expires"))
    authorized = bool(attempt.get("status") == "failed" and recovery.get("reason")
                      and recovery.get("after_count") == count and expires and now < expires)
    due = retry_at(attempt, task)
    return authorized or due is None or now >= due


def choose_task(status: dict, state: dict, now: datetime) -> dict:
    """한 번에 한 편. 수동 지시 우선, 작성 중·아침 대기 중인 일일 원고는 기다린다."""
    lease = state.get("lease") or {}
    expires = parsed_time(lease.get("expires"))
    if not status.get("synced") or status.get("locked_by") or (lease.get("owner") and expires and now < expires):
        return {}
    service_due = parsed_time((state.get("service_wait") or {}).get("until"))
    if service_due and now < service_due:
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
    ready = [{**base, **gap} for gap in status.get("daily") or []
             if retry_ready(state, {**base, **gap}, now)]
    # 실패한 첫 카테고리가 아직 시도하지 않은 나머지 카테고리를 가로막지 않는다.
    return min(ready, key=lambda task: (
        int(task_attempt(state, task).get("count") or 0),
        str(task_attempt(state, task).get("at") or "")), default={})


def reserve(state: dict, task: dict, owner: str, now: datetime) -> dict:
    attempts = {k: v for k, v in (state.get("attempts") or {}).items() if k.startswith(task["day"] + ":")}
    key = task_key(task)
    previous = task_attempt(state, task)
    attempts[key] = {**previous, "at": now.isoformat(timespec="seconds"), "count": int(previous.get("count") or 0) + 1,
                     "status": "writing"}
    for field in ("finished_at", "next_retry_at"):
        attempts[key].pop(field, None)
    authorizations = dict(state.get("retry_authorizations") or {})
    authorizations.pop(key, None)  # 사용자가 요청한 복구 재시도는 한 번만 허용하며 횟수 상한은 유지한다.
    return {**state, "attempts": attempts, "retry_authorizations": authorizations,
            "lease": {"owner": owner, "at": now.isoformat(timespec="seconds"),
                      "expires": (now + LEASE_TIME).isoformat(timespec="seconds"), "task": task}}


def settle(state: dict, task: dict, owner: str, result: str, note: str = "", *,
           now: datetime | None = None, failure_kind: str = "", rejected: list[dict] | None = None) -> dict:
    if (state.get("lease") or {}).get("owner") != owner:
        raise ValueError("Codex 클라우드 임대 주인이 바뀌었습니다")
    attempts = dict(state.get("attempts") or {})
    key = task_key(task)
    attempts[key] = {**attempts.get(key, {}), "status": result, "note": note[:300], "failure_kind": failure_kind}
    if now:
        attempts[key]["finished_at"] = now.isoformat(timespec="seconds")
    if rejected is not None:
        attempts[key]["rejected_candidates"] = rejected[-200:]
    due = retry_at(attempts[key], task) if result in ("failed", "skipped") else None
    attempts[key]["next_retry_at"] = due.isoformat(timespec="seconds") if due else None
    service_wait = state.get("service_wait") or {}
    if failure_kind in SERVICE_FAILURES and due:
        service_wait = {"failure_kind": failure_kind, "until": due.isoformat(timespec="seconds")}
    elif result == "done":
        service_wait = {}
    return {**state, "lease": {}, "attempts": attempts, "service_wait": service_wait}


def recover_terminal_lease(state: dict, run: dict, jobs: list[dict], *,
                           now: datetime | None = None) -> tuple[dict, dict]:
    """GitHub가 종료를 확인한 이전 실행만 해제한다. 실제 작성 시도는 횟수를 보존한다."""
    lease = state.get("lease") or {}
    if run.get("status") != "completed" or str(run.get("id")) != str(lease.get("owner", "")).split("-")[0]:
        return state, {}
    task = lease.get("task") or {}
    if not task.get("day") or "category" not in task:
        return state, {}
    # runner가 한 번이라도 배정됐으면 보수적으로 실제 작성 실패로 처리한다.
    writers = [job for job in jobs if job.get("name") == "write"]
    unstarted = not any(job.get("runner_id") for job in writers)
    key = task_key(task)
    attempt = (state.get("attempts") or {}).get(key) or {}
    result = "infrastructure_wait" if unstarted else "failed"
    note = "이전 GitHub 실행 종료 확인 — " + ("서버 미배정, 작성 횟수 복원" if unstarted else "중단된 임대 해제")
    recovered = settle(state, task, lease["owner"], result, note, now=now)
    if unstarted:
        recovered["attempts"][key]["count"] = max(0, int(attempt.get("count") or 0) - 1)
    return recovered, task


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
