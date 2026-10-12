"""클라우드 루틴용 원고 도우미 — 원고를 yoopost-drafts 브랜치에 올리면 GitHub Actions 가 슬랙 승인 요청을 보낸다.

클라우드 세션에는 비밀값이 없고 슬랙·구글에 접속할 수 없다. 이 스크립트는 git 만 쓴다.

    python scripts/cloud_drafts.py setup                     # .yoopost-drafts/ 에 원고 브랜치를 연다
    python scripts/cloud_drafts.py start [--network limited] # 오늘 실행 시작 기록 (슬랙 진행 막대가 올라감)
    python scripts/cloud_drafts.py status                    # 오늘 카테고리별 상태 (완료 / 남음 — 이유)
    python scripts/cloud_drafts.py missing [--json]          # 오늘 원고가 없는 카테고리 (건너뜀·전송 실패·원고 없음)
    python scripts/cloud_drafts.py claim --owner autofill    # 빈 카테고리 채우기 잠금 (한 번에 한 쪽만) / release 로 해제
    python scripts/cloud_drafts.py check <원고 폴더>          # 형식·분량·구성·사실 검증 + 기존 글과 겹침·최신 이슈 (문제 있으면 1)
    python scripts/cloud_drafts.py push <원고 폴더>           # 확인을 통과한 원고를 올림 → Actions 가 슬랙에 승인 요청
    python scripts/cloud_drafts.py skip --category "AI 공모주 분석" --reason "최근 14일 새 공모 없음"
    python scripts/cloud_drafts.py finish                    # 실행 끝 기록 (Actions 가 요약을 #yoopost-알림 에 보냄)
    python scripts/cloud_drafts.py dupcheck <원고 폴더>       # 이미 쓴 글·다른 원고와 겹치는지만 (겹치면 종료 코드 1)

슬랙 수동 지시(#yoopost-0-수동지시) 전용 — 매일 원고의 start·finish·skip·missing·claim·release 와 섞지 않는다:
    python scripts/cloud_drafts.py request start --id <요청 ID> [--category C] [--network limited]
    python scripts/cloud_drafts.py request status --id <요청 ID>      # 카테고리별 올린 원고·건너뜀·남은 카테고리 (JSON)
    python scripts/cloud_drafts.py push <원고 폴더> --request <요청 ID>
    python scripts/cloud_drafts.py skip --category C --reason R --request <요청 ID>
    python scripts/cloud_drafts.py request finish --id <요청 ID> [--note N]   # Actions 가 지시 스레드에 결과를 답함

원고 폴더는 scripts/claude_brief.py --new --drafts-dir .yoopost-drafts/drafts 로 만든다.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autopost.cloud_drafts import (  # noqa: E402
    BRANCH, DRAFTS, RUNS, SKIP_FILE, STATE, WORKTREE, draft_folders, folder_day, is_complete, is_parked, is_skip,
    missing_categories, novelty_problems, pushed_names, quality_report, read_json, recent_days, run_active,
    write_json,
)
from autopost.config import load_config  # noqa: E402
from autopost.codex_fallback import fallback_requests, takeover_record  # noqa: E402
from autopost.cloud_codex import cloud_lease  # noqa: E402
from autopost.daily_run import daily_health  # noqa: E402
from autopost.constants import CATEGORIES  # noqa: E402
from autopost.draft_request import (  # noqa: E402
    MAX_AGE_HOURS, read_request, request_id_of, request_path, with_request,
)
from autopost.history import HistoryEntry, PostHistory  # noqa: E402
from autopost.manual_command import KINDS  # noqa: E402
from autopost.pipeline import KST  # noqa: E402

TREE = ROOT / WORKTREE
PUSH_TRIES = 4


def git(*args: str, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and result.returncode != 0:
        raise SystemExit(f"❌ git {' '.join(args)} 실패: {(result.stderr or result.stdout).strip()[-500:]}")
    return result


SYNC = {"ok": False}  # 이번 실행에서 원고 브랜치를 원격과 맞췄는지 — 못 맞추면 빈 카테고리를 판단하지 않는다


def setup() -> Path:
    """원고 브랜치 작업 폴더를 열고 원격과 맞춘다 (받기 실패는 SYNC 에 남는다 — 옛 상태로 판단하지 않도록)."""
    if (TREE / ".git").exists():
        fetched = git("fetch", "-q", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}",
                      cwd=TREE, check=False).returncode == 0
        pulled = fetched and git("pull", "--rebase", "-q", "origin", BRANCH, cwd=TREE, check=False).returncode == 0
        if fetched and not pulled:
            git("rebase", "--abort", cwd=TREE, check=False)
        SYNC["ok"] = pulled
        return TREE
    git("fetch", "-q", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
    git("worktree", "add", "-q", "-B", BRANCH, str(TREE), f"origin/{BRANCH}")
    SYNC["ok"] = True
    return TREE


def pushed_folders() -> set[str]:
    """원격 원고 브랜치에 올라간 원고 폴더 이름 — 로컬에만 있는(올리지 못한) 원고는 채운 것으로 치지 않는다."""
    return pushed_names(TREE)


def publish(paths: list[str], message: str, guard=None) -> None:
    """원고 브랜치에 커밋하고 올린다 (다른 쪽이 먼저 올렸으면 받아서 다시)."""
    if guard:
        guard()
    git("add", "--", *paths, cwd=TREE)
    if git("diff", "--cached", "--quiet", cwd=TREE, check=False).returncode == 0:
        print("변경 없음 — 올릴 것이 없습니다")
        return
    git("commit", "-q", "-m", message, cwd=TREE)
    for attempt in range(1, PUSH_TRIES + 1):
        if git("pull", "--rebase", "-q", "origin", BRANCH, cwd=TREE, check=False).returncode != 0:
            git("rebase", "--abort", cwd=TREE, check=False)  # 충돌이면 내 커밋을 지키고 다시 시도
        else:
            if guard:
                guard()  # 인계가 pull/rebase 사이에 발생했으면 늦은 작성자의 push를 막는다.
            if git("push", "-q", "origin", f"HEAD:{BRANCH}", cwd=TREE, check=False).returncode == 0 and pushed():
                print(f"📤 올림: {message}")
                return
        time.sleep(3 * attempt)
    raise SystemExit("❌ 원고 브랜치에 올리지 못했습니다 (git push 실패)")


def pushed() -> bool:
    """원격 브랜치 끝이 내 커밋인지 — push 가 아무것도 안 하고 성공한 척하는 경우를 막는다."""
    head = git("rev-parse", "HEAD", cwd=TREE).stdout.strip()
    remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}", cwd=TREE, check=False).stdout.split()
    return bool(remote) and remote[0] == head


def today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def run_file() -> Path:
    return TREE / RUNS / f"{today()}.json"


def ensure_daily_owner() -> None:
    lease = cloud_lease(TREE, datetime.now(KST))
    task = lease.get("task") or {}
    mine = f"{os.environ.get('GITHUB_RUN_ID', '')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    if lease and lease.get("owner") == mine and not task.get("request") and task.get("day") == today():
        return
    if lease or (read_json(run_file()).get("fallback") or {}).get("provider") == "codex":
        raise SystemExit("Codex 클라우드가 일일 원고를 이어받았습니다 — 늦은 Claude 기록을 올리지 않습니다")


def ensure_daily_slot(category: str) -> None:
    ensure_daily_owner()
    if cloud_lease(TREE, datetime.now(KST)) and category not in [g.category for g in gaps()]:
        raise SystemExit("이 카테고리 원고가 이미 올라왔습니다 — Codex 중복 원고를 올리지 않습니다")


def start(network: str) -> None:
    ensure_daily_owner()
    now = datetime.now(KST).isoformat(timespec="seconds")
    write_json(run_file(), {"started": now, "finished": "", "network": network, "activity_at": now})
    publish([str(run_file().relative_to(TREE))], f"run: {today()} 시작", guard=ensure_daily_owner)


def heartbeat(category: str, phase: str) -> None:
    ensure_daily_owner()
    data = read_json(run_file())
    if not run_active(data):
        raise SystemExit("시작하지 않았거나 종료된 일일 실행입니다")
    write_json(run_file(), {**data, "activity_at": datetime.now(KST).isoformat(timespec="seconds"),
                            "category": category, "phase": phase})
    publish([str(run_file().relative_to(TREE))], f"run: {today()} {category} {phase}", guard=ensure_daily_owner)


def finish() -> None:
    ensure_daily_owner()
    data = read_json(run_file())
    write_json(run_file(), {**data, "finished": datetime.now(KST).isoformat(timespec="seconds")})
    publish([str(run_file().relative_to(TREE))], f"run: {today()} 끝", guard=ensure_daily_owner)


def gaps() -> list:
    return missing_categories(TREE / DRAFTS, today(), CATEGORIES, read_json(TREE / STATE / "failures.json"),
                              pushed_folders())


def status() -> int:
    """오늘 카테고리별 상태 — '완료'는 사실 검증까지 끝나 브랜치에 올라간 원고가 있는 것. 나머지는 이유와 함께 '남음'."""
    if not SYNC["ok"]:
        print("⚠️ 원고 브랜치를 원격과 맞추지 못해 옛 상태일 수 있습니다")
    empty = {g.number: g for g in gaps()}
    folders = draft_folders(TREE / DRAFTS, [today()])
    for number, category in enumerate(CATEGORIES, start=1):
        if number in empty:
            print(f"{number}. {category}: 남음 — {empty[number].reason}")
            continue
        names = [f.name for f in folders if f.name.endswith(f"_{number}") and not is_skip(f) and not request_id_of(f)]
        print(f"{number}. {category}: 완료  ({', '.join(names)})")
    return 0


CLOUD_WAIT_UNTIL = (7, 15)  # 07시 Claude 루틴이 15분 동안 시작되지 않으면 독립 Codex가 인계한다.
LOCK_HOURS = 3  # 채우기 잠금이 이보다 오래되면 멈춘 것으로 보고 풀어 준다


def cloud_running() -> bool:
    """시작 기록과 마지막 일일 원고 업로드·진행 기록으로 판단한다."""
    return daily_health(TREE, datetime.now(KST))["running"]


def cloud_pending() -> bool:
    """오늘 실행 기록이 아직 없고 기다릴 시각 전이다 — 앱을 07시 전에 켜서 보충이 먼저 도는 것을 막는다."""
    if run_file().is_file():
        return False
    now = datetime.now(KST)
    return (now.hour, now.minute) < CLOUD_WAIT_UNTIL


def lock_path() -> Path:
    return TREE / ".fill.lock"


def lock_holder() -> dict:
    """채우기 잠금을 가진 쪽 (없거나 오래되어 풀린 잠금이면 빈 dict)."""
    remote = cloud_lease(TREE, datetime.now(KST))
    if remote:
        return {**remote, "owner": "codex-cloud"}
    data = read_json(lock_path())
    try:
        at = datetime.fromisoformat(str(data["at"]))
    except (KeyError, ValueError):
        return {}
    return data if datetime.now(KST) - at < timedelta(hours=LOCK_HOURS) else {}


def claim(owner: str) -> int:
    """빈 카테고리 채우기를 한 번에 한 쪽만 하도록 잠근다 (예약 작업·수동 채우기가 같은 카테고리를 겹쳐 쓰지 않게)."""
    holder = lock_holder()
    if holder and holder.get("owner") != owner:
        print(f"🔒 이미 채우는 중: {holder.get('owner')} ({holder.get('at')})")
        return 3
    lock_path().unlink(missing_ok=True)
    try:
        fd = os.open(lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print("🔒 다른 쪽이 방금 잠갔습니다")
        return 3
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"owner": owner, "at": datetime.now(KST).isoformat(timespec="seconds")}, handle, ensure_ascii=False)
    print(f"🔓→🔒 잠금: {owner}")
    return 0


def release(owner: str) -> int:
    holder = read_json(lock_path())
    if holder and holder.get("owner") not in (owner, None):
        print(f"잠금 주인이 다릅니다: {holder.get('owner')} — 그대로 둡니다")
        return 3
    lock_path().unlink(missing_ok=True)
    print("🔓 잠금 해제")
    return 0


def missing(as_json: bool) -> int:
    """오늘 원고가 없는 카테고리 (건너뜀·전송 실패·올리지 못함·원고 없음) — 다시 쓰거나 자동 보충할 대상.

    원고 브랜치를 원격과 맞추지 못했으면 판단하지 않는다 (옛 상태로 '전부 빈 카테고리'라고 착각하지 않도록, 종료 코드 2).
    """
    if not SYNC["ok"]:
        message = "원고 브랜치를 원격과 맞추지 못했습니다 (git fetch/pull 실패) — 빈 카테고리를 판단하지 않습니다"
        print(json.dumps({"synced": False, "error": message}, ensure_ascii=False) if as_json else f"❌ {message}")
        return 2
    found = gaps()
    state = {"synced": True, "cloud_running": cloud_running(), "cloud_pending": cloud_pending(),
             "locked_by": lock_holder().get("owner", "")}
    if as_json:
        print(json.dumps({**state, "gaps": [
            {"number": g.number, "category": g.category, "reason": g.reason} for g in found]}, ensure_ascii=False))
        return 0
    if state["cloud_running"]:
        print("☁️ 오늘 클라우드 실행 기록이 '진행 중'입니다 — 클라우드 루틴 자신이라면 아래 카테고리를 다시 시도하세요 "
              "(로컬 자동 보충은 클라우드가 끝난 뒤 채웁니다)")
    if not found:
        print("빈 카테고리 없음 — 오늘 7개 카테고리 모두 원고가 있습니다")
    else:
        for g in found:
            print(f"{g.number}. {g.category} — {g.reason}")
    return 0


def check(folder: Path, request: str = "") -> int:
    """형식·분량·구성·사실 검증 기록 + 기존 글과 겹침·최신 이슈 (2026-10-01 사용자 지시) 확인."""
    folder = folder.resolve()
    report = quality_report(folder, load_config().min_body_chars)
    problems = [*report.problems, *novelty_problems(folder, TREE, datetime.now(KST), pushed_folders(), request)]
    print(f"📋 {folder.name}: {report.stats}")
    for problem in problems:
        print(f"   ❌ {problem}")
    print("✅ 올릴 수 있습니다" if not problems else "고친 뒤 다시 확인하세요 (겹치거나 오래된 소식이면 주제를 바꾼다)")
    return 0 if not problems else 1


def folder_category(folder: Path) -> str:
    """원고 폴더 이름 끝 번호의 카테고리 (번호가 없거나 0 이면 빈 문자열)."""
    suffix = folder.name.rsplit("_", 1)[-1]
    return CATEGORIES[int(suffix) - 1] if suffix.isdigit() and 1 <= int(suffix) <= len(CATEGORIES) else ""


def request_problem(folder: Path, rid: str) -> tuple[int, str] | None:
    """수동 지시 원고를 올려도 되는지 — (종료 코드, 이유) 또는 None. 3 = 이미 올림, 4 = 요청·카테고리가 맞지 않음."""
    record = read_request(TREE, rid)
    if not record:
        return 4, f"요청 기록이 없습니다: {rid} (request start 를 먼저)"
    if not record.get("started"):
        return 4, "request start 를 먼저 해야 합니다"
    if record.get("finished"):
        return 4, "이미 끝난 요청입니다 (request finish 뒤에는 올리지 않음)"
    category = folder_category(folder)
    if not category:
        return 4, "카테고리 번호가 없는 원고 폴더입니다 — claude_brief.py 에 --category 를 주고 만드세요"
    posted = str(read_json(folder / "post.json").get("category") or "")
    if posted != category:
        return 4, f"post.json category '{posted}' 가 폴더 카테고리 '{category}' 와 다릅니다"
    if record.get("kind") == "single" and record.get("category") and record["category"] != category:
        return 4, f"이 요청의 카테고리는 '{record['category']}' 입니다"
    pushed, failures = pushed_folders(), read_json(TREE / STATE / "failures.json")
    mine = [f for f in draft_folders(TREE / DRAFTS, recent_days(datetime.now(KST)))  # Actions 가 멈춘 원고는 대체할 수 있게
            if f.name != folder.name and f.name in pushed and not is_skip(f) and request_id_of(f) == rid
            and not is_parked(f, failures)]
    if record.get("kind") == "single" and mine:
        return 3, f"이 요청(1편)으로 이미 올린 원고가 있습니다: {mine[0].name}"
    same = [f.name for f in mine if folder_category(f) == category]
    if same:
        return 3, f"이 요청으로 이 카테고리 원고를 이미 올렸습니다: {same[0]}"
    return None


def push(folder: Path, request: str = "") -> int:
    folder = folder.resolve()
    if folder.parent != (TREE / DRAFTS).resolve():
        raise SystemExit(f"❌ 원고 폴더는 {TREE / DRAFTS} 안에 있어야 합니다: {folder}")
    if not request:
        ensure_daily_slot(folder_category(folder))
    if request:
        problem = request_problem(folder, request)
        if problem:
            print(f"❌ {problem[1]}")
            return problem[0]
        research = folder / "research.json"  # 표시는 모델이 아니라 이 도우미가 붙인다
        try:  # 깨진 파일을 표시만 남긴 파일로 덮어쓰지 않도록 엄격하게 읽는다
            data = json.loads(research.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"❌ research.json 을 읽지 못했습니다 ({exc}) — 고친 뒤 다시 올리세요")
            return 1
        if not isinstance(data, dict):
            print("❌ research.json 이 객체(JSON object)가 아닙니다")
            return 1
        write_json(research, with_request(data, request, str(read_request(TREE, request)["kind"])))
    if check(folder, request) != 0:
        return 1
    title = str(read_json(folder / "post.json").get("title") or folder.name)[:80]
    files = [str((folder / name).relative_to(TREE)) for name in ("post.json", "research.json")]
    publish(files, f"draft: {folder.name} {'🧑‍💼 ' if request else ''}{title}",
            guard=None if request else lambda: ensure_daily_slot(folder_category(folder)))
    return 0


def skip(category: str, reason: str, request: str = "") -> int:
    """건너뜀 기록 — 매일 원고면 빈 카테고리로 남아 재시도·자동 보충 대상, 수동 지시면 지시 스레드에 이유를 알린다."""
    if not request:
        ensure_daily_slot(category)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    folder = TREE / DRAFTS / f"{stamp}_{CATEGORIES.index(category) + 1}"
    data = {"category": category, "reason": reason, "at": datetime.now(KST).isoformat(timespec="seconds")}
    if request:
        record = read_request(TREE, request)
        if not record:
            print(f"❌ 요청 기록이 없습니다: {request}")
            return 4
        data = with_request(data, request, str(record.get("kind") or "single"))
    write_json(folder / SKIP_FILE, data)
    publish([str((folder / SKIP_FILE).relative_to(TREE))], f"skip: {category} — {reason}"[:120],
            guard=None if request else lambda: ensure_daily_slot(category))
    return 0


def dupcheck(folder: Path) -> int:
    """이미 쓴 글(state/published.json 전부)·원고 브랜치에 올라간 다른 원고와 겹치는지 (Actions 의 중복 확인을 미리).

    올리지 못하고 남은 로컬 폴더(첫 시도 실패 등)·Actions 가 멈춘 원고(중복 거절 등)는 비교하지 않는다 — 재시도를 막지 않도록.
    """
    folder = folder.resolve()
    overlap = [p for p in novelty_problems(folder, TREE, datetime.now(KST), pushed_folders())
               if p.startswith(("이미 쓴 글", "post.json·research.json"))]
    if overlap:
        print(f"❌ {overlap[0]}")
        return 1
    print("✅ 겹치는 글 없음")
    return 0


def _parse_time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def request_start(rid: str, category: str, network: str) -> int:
    """수동 지시 시작 — Actions 가 남긴 요청 기록과 맞아야 한다. 0 = 시작, 3 = 이미 시작·끝·오래됨(중복 실행), 4 = 없음·불일치."""
    if cloud_lease(TREE, datetime.now(KST)):
        print("⏹️ Codex 클라우드가 이어 쓰는 중입니다")
        return 3
    record = read_request(TREE, rid)
    if not record or record.get("kind") not in KINDS:
        print(f"❌ 요청 기록이 없거나 형식이 틀립니다: {rid}")
        return 4
    if record.get("started") or record.get("finished") or record.get("fire_error"):
        print("⏹️ 이미 시작했거나 끝났거나 실행에 실패해 닫힌 요청입니다 — 아무것도 하지 않습니다")
        return 3
    created = _parse_time(record.get("created"))
    if created is None or datetime.now(KST) - created > timedelta(hours=MAX_AGE_HOURS):
        print(f"⏹️ {MAX_AGE_HOURS}시간이 지난 요청입니다 — 아무것도 하지 않습니다")
        return 3
    chosen = str(record.get("category") or "")
    if record["kind"] == "single":
        if chosen and category and category != chosen:
            print(f"❌ 이 요청의 카테고리는 '{chosen}' 입니다")
            return 4
        chosen = chosen or category
        if chosen not in CATEGORIES:
            print("❌ 카테고리가 정해지지 않은 요청입니다 — 주제에 맞는 카테고리를 --category 로 정해 다시 실행하세요")
            return 4
    updated = {**record, "category": chosen, "started": datetime.now(KST).isoformat(timespec="seconds"),
               "network": network}
    path = request_path(TREE, rid)
    write_json(path, updated)
    publish([str(path.relative_to(TREE))], f"request: {rid} 시작")
    print(json.dumps({k: updated.get(k, "") for k in ("id", "kind", "category", "topic")}, ensure_ascii=False))
    return 0


def request_status(rid: str) -> int:
    """이 요청으로 올린 원고·건너뜀·남은 카테고리 (JSON)."""
    record = read_request(TREE, rid)
    if not record:
        print(json.dumps({"id": rid, "error": "요청 기록이 없습니다"}, ensure_ascii=False))
        return 4
    pushed, failures = pushed_folders(), read_json(TREE / STATE / "failures.json")
    folders = [f for f in draft_folders(TREE / DRAFTS, recent_days(datetime.now(KST))) if request_id_of(f) == rid]
    wanted = list(CATEGORIES) if record.get("kind") == "all" else [str(record.get("category") or "")]
    rows = []
    for category in wanted:
        mine = [f for f in folders if folder_category(f) == category]
        drafts = [f.name for f in mine if f.name in pushed and is_complete(f) and not is_parked(f, failures)]
        stopped = [f"{f.name}: {failures[f.name].get('reason') or ''}" for f in mine if is_parked(f, failures)]
        skips = [str(read_json(f / SKIP_FILE).get("reason") or "") for f in mine if is_skip(f)]
        rows.append({"category": category, "drafts": drafts, "skip": skips[-1] if skips else "", "stopped": stopped})
    print(json.dumps({"id": rid, "kind": record.get("kind"), "started": record.get("started", ""),
                      "finished": record.get("finished", ""), "categories": rows,
                      "missing": [r["category"] for r in rows if not r["drafts"]]}, ensure_ascii=False))
    return 0


def request_finish(rid: str, note: str) -> int:
    record = read_request(TREE, rid)
    if not record:
        print(f"❌ 요청 기록이 없습니다: {rid}")
        return 4
    path = request_path(TREE, rid)
    write_json(path, {**record, "finished": datetime.now(KST).isoformat(timespec="seconds"), "note": note[:300]})
    publish([str(path.relative_to(TREE))], f"request: {rid} 끝")
    return 0


def fallback_status() -> int:
    """Codex 예약 확인용 JSON. 동기화 실패 시 옛 기록으로 인계하지 않는다."""
    if not SYNC["ok"]:
        print(json.dumps({"synced": False, "error": "원고 브랜치 동기화 실패"}, ensure_ascii=False))
        return 2
    print(json.dumps({"synced": True, "locked_by": lock_holder().get("owner", ""),
                      "cloud_running": cloud_running(), "cloud_pending": cloud_pending(),
                      "claude": daily_health(TREE, datetime.now(KST)),
                      "daily": [{"category": g.category, "reason": g.reason} for g in gaps()],
                      "requests": fallback_requests(TREE, datetime.now(KST), pushed_folders())}, ensure_ascii=False))
    return 0


def fallback_resume(rid: str, category: str) -> int:
    """Codex가 잠금을 잡은 뒤에만 실패·중단된 수동 지시를 재개한다."""
    if not SYNC["ok"]:
        print("❌ 원고 브랜치 동기화 실패 — 인계하지 않습니다")
        return 2
    if lock_holder().get("owner") != "codex-fallback":
        print("❌ claim --owner codex-fallback 을 먼저 실행하세요")
        return 3
    candidate = next((r for r in fallback_requests(TREE, datetime.now(KST), pushed_folders()) if r["id"] == rid), None)
    if candidate is None:
        print("⏹️ 인계 대상이 아닙니다 — 실행 중·종료·오래된 요청은 그대로 둡니다")
        return 3
    try:
        updated = takeover_record(read_request(TREE, rid), datetime.now(KST), candidate["reason"], category)
    except ValueError as exc:
        print(f"❌ {exc}")
        return 4
    path = request_path(TREE, rid)
    write_json(path, updated)
    publish([str(path.relative_to(TREE))], f"request: {rid} Codex 인계")
    print(json.dumps({**candidate, "category": updated["category"]}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="클라우드 원고 → yoopost-drafts 브랜치")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    start_p = sub.add_parser("start")
    start_p.add_argument("--network", default="full", choices=["full", "limited"])
    heartbeat_p = sub.add_parser("heartbeat", help="일일 Claude 작성 활동 기록")
    heartbeat_p.add_argument("--category", required=True, choices=CATEGORIES)
    heartbeat_p.add_argument("--phase", default="조사·작성", choices=["조사·작성", "사실 검증", "전송 준비"])
    sub.add_parser("status")
    sub.add_parser("missing").add_argument("--json", action="store_true", help="JSON 으로 출력")
    for name in ("claim", "release"):
        sub.add_parser(name).add_argument("--owner", default="local", help="채우는 쪽 이름 (예: autofill, manual)")
    sub.add_parser("finish")
    check_p = sub.add_parser("check")
    check_p.add_argument("folder", type=Path)
    check_p.add_argument("--request", default="", help="슬랙 수동 지시 요청 ID (주제를 정한 지시는 최신 이슈 조건을 끔)")
    sub.add_parser("dupcheck").add_argument("folder", type=Path)
    push_p = sub.add_parser("push")
    push_p.add_argument("folder", type=Path)
    push_p.add_argument("--request", default="", help="슬랙 수동 지시 요청 ID (원고에 수동 지시 표시를 붙임)")
    skip_p = sub.add_parser("skip")
    skip_p.add_argument("--category", required=True, choices=CATEGORIES)
    skip_p.add_argument("--reason", required=True)
    skip_p.add_argument("--request", default="", help="슬랙 수동 지시 요청 ID")
    request_p = sub.add_parser("request", help="슬랙 수동 지시 실행 기록")
    request_sub = request_p.add_subparsers(dest="action", required=True)
    request_start_p = request_sub.add_parser("start")
    request_start_p.add_argument("--id", required=True)
    request_start_p.add_argument("--category", default="", choices=["", *CATEGORIES])
    request_start_p.add_argument("--network", default="full", choices=["full", "limited"])
    request_sub.add_parser("status").add_argument("--id", required=True)
    request_finish_p = request_sub.add_parser("finish")
    request_finish_p.add_argument("--id", required=True)
    request_finish_p.add_argument("--note", default="")
    fallback_p = sub.add_parser("fallback", help="Claude 한도 오류·중단 시 Codex 인계")
    fallback_sub = fallback_p.add_subparsers(dest="action", required=True)
    fallback_sub.add_parser("status")
    resume_p = fallback_sub.add_parser("resume")
    resume_p.add_argument("--id", required=True)
    resume_p.add_argument("--category", default="", choices=["", *CATEGORIES])
    args = parser.parse_args(argv)
    if args.command == "setup":
        print(f"원고 브랜치: {setup()}")
        if not SYNC["ok"]:
            print("❌ 원격과 맞추지 못했습니다 (git fetch/pull 실패) — 네트워크·권한을 확인하세요")
            return 2
        return 0
    if args.command == "check":
        return check(args.folder, args.request)
    setup()
    if args.command in ("start", "heartbeat", "finish", "push", "skip") and not SYNC["ok"]:
        print("❌ 원고 브랜치 동기화 실패 — 작성 기록을 올리지 않습니다")
        return 2
    if args.command == "fallback":
        return fallback_status() if args.action == "status" else fallback_resume(args.id, args.category)
    if args.command == "start":
        start(args.network)
        return 0
    if args.command == "heartbeat":
        heartbeat(args.category, args.phase)
        return 0
    if args.command == "finish":
        finish()
        return 0
    if args.command == "status":
        return status()
    if args.command == "missing":
        return missing(args.json)
    if args.command == "claim":
        return claim(args.owner)
    if args.command == "release":
        return release(args.owner)
    if args.command == "push":
        return push(args.folder, args.request)
    if args.command == "dupcheck":
        return dupcheck(args.folder)
    if args.command == "request":
        if not SYNC["ok"]:
            print("❌ 원고 브랜치를 원격과 맞추지 못했습니다 — 요청 기록을 판단하지 않습니다")
            return 2
        if args.action == "start":
            return request_start(args.id, args.category, args.network)
        if args.action == "status":
            return request_status(args.id)
        return request_finish(args.id, args.note)
    return skip(args.category, args.reason, args.request)


if __name__ == "__main__":
    sys.exit(main())
