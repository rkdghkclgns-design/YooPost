"""일일 Claude 실행의 실제 활동을 기준으로 중단과 인계를 판단한다."""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

IDLE_LIMIT = timedelta(minutes=60)
RUN_LIMIT = timedelta(hours=3)


def parsed(value, now: datetime) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return moment if moment.tzinfo and moment <= now else None
    except (TypeError, ValueError):
        return None


def daily_health(tree: Path, now: datetime) -> dict:
    # 지연 import: cloud_drafts의 전송 처리에서도 같은 판정을 사용한다.
    from .cloud_drafts import DRAFTS, RUNS, draft_folders, read_json
    from .draft_request import request_id_of

    day = now.strftime("%Y%m%d")
    path = tree / RUNS / f"{day}.json"
    run = read_json(path)
    base = {"running": False, "category": run.get("category", ""), "phase": run.get("phase", "")}
    if (run.get("fallback") or {}).get("provider") == "codex":
        return {**base, "status": "handed_off", "reason": "Codex 클라우드 인계"}
    if run.get("finished"):
        return {**base, "status": "finished", "reason": "실행 종료"}
    started = parsed(run.get("started"), now)
    if not started:
        return {**base, "status": "absent", "reason": "실행 기록 없음"}
    activity = max(started, parsed(run.get("activity_at"), now) or started)
    # checkout 수정 시각이나 state/ 갱신은 작성 활동이 아니다. 과거 작성기도
    # 지원하도록 일일 run·원고 파일의 실제 원격 커밋 시각을 확인한다.
    paths = [path.relative_to(tree).as_posix()]
    for folder in draft_folders(tree / DRAFTS, [day]):
        if not request_id_of(folder):
            paths.extend((folder / name).relative_to(tree).as_posix()
                         for name in ("post.json", "research.json", "skip.json"))
    try:
        shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=tree,
                                 capture_output=True, text=True, timeout=10)
        # 얕은 checkout의 마지막 커밋은 모든 파일을 새로 만든 것으로 보여 오판한다.
        if shallow.returncode == 0 and shallow.stdout.strip() == "false":
            result = subprocess.run(["git", "log", "-1", "--format=%cI", "origin/yoopost-drafts", "--", *paths],
                                    cwd=tree, capture_output=True, text=True, timeout=10)
            committed = parsed(result.stdout.strip(), now) if result.returncode == 0 else None
            if committed:
                activity = max(activity, committed)
    except (OSError, subprocess.TimeoutExpired):
        pass
    idle = max(0, int((now - activity).total_seconds() // 60))
    running = now - activity < IDLE_LIMIT and now - started < RUN_LIMIT
    return {**base, "running": running, "status": "active" if running else "stalled",
            "activity_at": activity.isoformat(timespec="seconds"), "idle_minutes": idle,
            "reason": "작성 활동 확인" if running else
            f"마지막 작성 활동 이후 {idle}분 경과" if now - activity >= IDLE_LIMIT else "실행 3시간 경과"}
