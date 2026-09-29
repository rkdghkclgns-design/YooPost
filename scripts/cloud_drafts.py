"""클라우드 루틴용 원고 도우미 — 원고를 yoopost-drafts 브랜치에 올리면 GitHub Actions 가 슬랙 승인 요청을 보낸다.

클라우드 세션에는 비밀값이 없고 슬랙·구글에 접속할 수 없다. 이 스크립트는 git 만 쓴다.

    python scripts/cloud_drafts.py setup                     # .yoopost-drafts/ 에 원고 브랜치를 연다
    python scripts/cloud_drafts.py start [--network limited] # 오늘 실행 시작 기록 (슬랙 진행 막대가 올라감)
    python scripts/cloud_drafts.py status                    # 오늘 카테고리별 상태 (이미 올린 카테고리는 건너뜀)
    python scripts/cloud_drafts.py check <원고 폴더>          # 형식·분량·구성·사실 검증 기록 확인 (문제 있으면 종료 코드 1)
    python scripts/cloud_drafts.py push <원고 폴더>           # 확인을 통과한 원고를 올림 → Actions 가 슬랙에 승인 요청
    python scripts/cloud_drafts.py skip --category "AI 공모주 분석" --reason "최근 60일 공모 없음"
    python scripts/cloud_drafts.py finish                    # 실행 끝 기록 (Actions 가 요약을 #yoopost-알림 에 보냄)

원고 폴더는 scripts/claude_brief.py --new --drafts-dir .yoopost-drafts/drafts 로 만든다.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autopost.cloud_drafts import (  # noqa: E402
    BRANCH, DRAFTS, RUNS, SKIP_FILE, WORKTREE, draft_folders, is_skip, quality_report, read_json, write_json,
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


def setup() -> Path:
    if (TREE / ".git").exists():
        git("pull", "--rebase", "-q", "origin", BRANCH, cwd=TREE, check=False)
        return TREE
    git("fetch", "-q", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
    git("worktree", "add", "-q", "-B", BRANCH, str(TREE), f"origin/{BRANCH}")
    return TREE


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


def status() -> int:
    folders = draft_folders(TREE / DRAFTS, [today()])
    for number, category in enumerate(CATEGORIES, start=1):
        mine = [f for f in folders if f.name.endswith(f"_{number}")]
        done = [f for f in mine if is_skip(f) or read_json(f / "research.json").get("factcheck")]
        state = "완료(건너뜀)" if any(is_skip(f) for f in mine) else "완료" if done else "남음"
        print(f"{number}. {category}: {state}" + (f"  ({', '.join(f.name for f in done)})" if done else ""))
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
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    folder = TREE / DRAFTS / f"{stamp}_{CATEGORIES.index(category) + 1}"
    write_json(folder / SKIP_FILE, {"category": category, "reason": reason})
    publish([str((folder / SKIP_FILE).relative_to(TREE))], f"skip: {category} — {reason}"[:120])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="클라우드 원고 → yoopost-drafts 브랜치")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    start_p = sub.add_parser("start")
    start_p.add_argument("--network", default="full", choices=["full", "limited"])
    sub.add_parser("status")
    sub.add_parser("finish")
    for name in ("check", "push"):
        sub.add_parser(name).add_argument("folder", type=Path)
    skip_p = sub.add_parser("skip")
    skip_p.add_argument("--category", required=True, choices=CATEGORIES)
    skip_p.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if args.command == "setup":
        print(f"원고 브랜치: {setup()}")
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
    if args.command == "push":
        return push(args.folder)
    return skip(args.category, args.reason)


if __name__ == "__main__":
    sys.exit(main())
