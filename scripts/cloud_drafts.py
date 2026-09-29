"""클라우드 루틴용 원고 도우미 — 원고를 yoopost-drafts 브랜치에 올리면 GitHub Actions 가 슬랙 승인 요청을 보낸다.

클라우드 세션에는 비밀값이 없고 슬랙·구글에 접속할 수 없다. 이 스크립트는 git 만 쓴다.

    python scripts/cloud_drafts.py setup                     # .yoopost-drafts/ 에 원고 브랜치를 연다
    python scripts/cloud_drafts.py start [--network limited] # 오늘 실행 시작 기록 (슬랙 진행 막대가 올라감)
    python scripts/cloud_drafts.py status                    # 오늘 카테고리별 상태 (완료 / 남음 — 이유)
    python scripts/cloud_drafts.py missing [--json]          # 오늘 원고가 없는 카테고리 (건너뜀·전송 실패·원고 없음)
    python scripts/cloud_drafts.py claim --owner autofill    # 빈 카테고리 채우기 잠금 (한 번에 한 쪽만) / release 로 해제
    python scripts/cloud_drafts.py check <원고 폴더>          # 형식·분량·구성·사실 검증 기록 확인 (문제 있으면 종료 코드 1)
    python scripts/cloud_drafts.py push <원고 폴더>           # 확인을 통과한 원고를 올림 → Actions 가 슬랙에 승인 요청
    python scripts/cloud_drafts.py skip --category "AI 공모주 분석" --reason "최근 60일 공모 없음"
    python scripts/cloud_drafts.py finish                    # 실행 끝 기록 (Actions 가 요약을 #yoopost-알림 에 보냄)

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
    BRANCH, DRAFTS, RUNS, SKIP_FILE, STATE, WORKTREE, draft_folders, is_skip, missing_categories, quality_report,
    read_json, run_active, write_json,
)
from autopost.config import load_config  # noqa: E402
from autopost.constants import CATEGORIES  # noqa: E402
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
    listing = git("ls-tree", "-r", "--name-only", f"origin/{BRANCH}", "--", f"{DRAFTS}/", cwd=TREE, check=False)
    names = set()
    for line in listing.stdout.splitlines():
        parts = line.strip().split("/")
        if len(parts) == 3 and parts[2] in ("post.json", "research.json", SKIP_FILE):
            names.add(parts[1])
    return names


def publish(paths: list[str], message: str) -> None:
    """원고 브랜치에 커밋하고 올린다 (다른 쪽이 먼저 올렸으면 받아서 다시)."""
    git("add", "--", *paths, cwd=TREE)
    if git("diff", "--cached", "--quiet", cwd=TREE, check=False).returncode == 0:
        print("변경 없음 — 올릴 것이 없습니다")
        return
    git("commit", "-q", "-m", message, cwd=TREE)
    for attempt in range(1, PUSH_TRIES + 1):
        if git("pull", "--rebase", "-q", "origin", BRANCH, cwd=TREE, check=False).returncode != 0:
            git("rebase", "--abort", cwd=TREE, check=False)  # 충돌이면 내 커밋을 지키고 다시 시도
        elif git("push", "-q", "origin", f"HEAD:{BRANCH}", cwd=TREE, check=False).returncode == 0 and pushed():
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


def start(network: str) -> None:
    now = datetime.now(KST).isoformat(timespec="seconds")
    write_json(run_file(), {"started": now, "finished": "", "network": network})
    publish([str(run_file().relative_to(TREE))], f"run: {today()} 시작")


def finish() -> None:
    data = read_json(run_file())
    write_json(run_file(), {**data, "finished": datetime.now(KST).isoformat(timespec="seconds")})
    publish([str(run_file().relative_to(TREE))], f"run: {today()} 끝")


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
        names = [f.name for f in folders if f.name.endswith(f"_{number}") and not is_skip(f)]
        print(f"{number}. {category}: 완료  ({', '.join(names)})")
    return 0


CLOUD_RUN_HOURS = 3  # 이 시간 안에 시작해 아직 끝나지 않은 클라우드 실행은 '진행 중' (그 뒤는 멈춘 것으로 본다)
CLOUD_WAIT_UNTIL = (10, 30)  # 실행 기록이 없어도 이 시각(KST) 전에는 클라우드를 기다린다 (워치독과 같은 시각)
LOCK_HOURS = 3  # 채우기 잠금이 이보다 오래되면 멈춘 것으로 보고 풀어 준다


def cloud_running() -> bool:
    """오늘 클라우드 실행이 진행 중인지 — 로컬 자동 보충이 같은 카테고리를 겹쳐 쓰지 않도록."""
    run = read_json(run_file())
    if not run_active(run):
        return False
    try:
        started = datetime.fromisoformat(str(run["started"]))
    except (KeyError, ValueError):
        return False
    return datetime.now(KST) - started < timedelta(hours=CLOUD_RUN_HOURS)


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


def check(folder: Path) -> int:
    report = quality_report(folder, load_config().min_body_chars)
    print(f"📋 {folder.name}: {report.stats}")
    for problem in report.problems:
        print(f"   ❌ {problem}")
    print("✅ 올릴 수 있습니다" if report.ok else "고친 뒤 다시 확인하세요")
    return 0 if report.ok else 1


def push(folder: Path) -> int:
    folder = folder.resolve()
    if folder.parent != (TREE / DRAFTS).resolve():
        raise SystemExit(f"❌ 원고 폴더는 {TREE / DRAFTS} 안에 있어야 합니다: {folder}")
    if check(folder) != 0:
        return 1
    title = str(read_json(folder / "post.json").get("title") or folder.name)[:80]
    files = [str((folder / name).relative_to(TREE)) for name in ("post.json", "research.json")]
    publish(files, f"draft: {folder.name} {title}")
    return 0


def skip(category: str, reason: str) -> int:
    """건너뜀 기록 — 빈 카테고리로 남아 클라우드의 재시도·로컬 자동 보충 대상이 된다."""
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    folder = TREE / DRAFTS / f"{stamp}_{CATEGORIES.index(category) + 1}"
    write_json(folder / SKIP_FILE, {"category": category, "reason": reason, "at": datetime.now(KST).isoformat(timespec="seconds")})
    publish([str((folder / SKIP_FILE).relative_to(TREE))], f"skip: {category} — {reason}"[:120])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="클라우드 원고 → yoopost-drafts 브랜치")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    start_p = sub.add_parser("start")
    start_p.add_argument("--network", default="full", choices=["full", "limited"])
    sub.add_parser("status")
    sub.add_parser("missing").add_argument("--json", action="store_true", help="JSON 으로 출력")
    for name in ("claim", "release"):
        sub.add_parser(name).add_argument("--owner", default="local", help="채우는 쪽 이름 (예: autofill, manual)")
    sub.add_parser("finish")
    for name in ("check", "push"):
        sub.add_parser(name).add_argument("folder", type=Path)
    skip_p = sub.add_parser("skip")
    skip_p.add_argument("--category", required=True, choices=CATEGORIES)
    skip_p.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if args.command == "setup":
        print(f"원고 브랜치: {setup()}")
        if not SYNC["ok"]:
            print("❌ 원격과 맞추지 못했습니다 (git fetch/pull 실패) — 네트워크·권한을 확인하세요")
            return 2
        return 0
    if args.command == "check":
        return check(args.folder)
    setup()
    if args.command == "start":
        start(args.network)
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
        return push(args.folder)
    return skip(args.category, args.reason)


if __name__ == "__main__":
    sys.exit(main())
