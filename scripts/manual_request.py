"""GitHub Actions manual_request.yml 진입점 — 슬랙 수동 지시 하나를 처리한다 (autopost/manual_request.py).

입력은 모두 환경 변수: MANUAL_TEXT · MANUAL_CHANNEL · MANUAL_TS · MANUAL_USER · MANUAL_SOURCE,
SLACK_CHANNEL_MANUAL, ROUTINE_FIRE_URL(변수) · ROUTINE_FIRE_TOKEN(비밀), SLACK_BOT_TOKEN·SLACK_CHANNEL_ID(비밀, #yoopost-알림).
비밀값은 출력하지 않는다.
루틴을 깨우기 전에 요청 기록을 원고 브랜치(--tree, 기본 .yoopost-drafts)의 runs/requests/<요청 ID>.json 에 올린다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autopost.cloud_drafts import BRANCH, WORKTREE  # noqa: E402
from autopost.draft_request import request_path  # noqa: E402
from autopost.manual_request import fire_routine, handle_request  # noqa: E402
from autopost.slack_helper import SlackNotifier  # noqa: E402

PUSH_TRIES = 4


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def dispatch_codex(rid: str) -> bool:
    """인증이 설정된 클라우드 작성기를 즉시 깨운다. 실제 인계 판단은 Codex plan이 다시 한다."""
    if env("CODEX_FALLBACK_ENABLED") != "true" or not env("GITHUB_TOKEN"):
        return False
    import requests
    repo = env("GITHUB_REPOSITORY")
    if repo != "rkdghkclgns-design/YooPost":
        return False
    try:
        response = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/codex_fallback.yml/dispatches",
            headers={"Authorization": f"Bearer {env('GITHUB_TOKEN')}", "Accept": "application/vnd.github+json"},
            json={"ref": "main", "inputs": {"mode": "work"}}, timeout=20)
        return response.status_code == 204
    except requests.RequestException:
        return False


def git(tree: Path, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=tree, capture_output=True, text=True).returncode == 0


def record_to_branch(tree: Path, record: dict) -> None:
    """요청 기록을 원고 브랜치에 올린다 (다른 쪽이 먼저 올렸으면 받아서 다시). 실패하면 예외 — 루틴을 깨우지 않는다."""
    path = request_path(tree, record["id"])
    if path.exists() and not record.get("fire_error"):  # 실행 실패 표시만 같은 기록에 덧쓴다
        raise RuntimeError(f"이미 기록된 요청입니다: {record['id']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    git(tree, "config", "user.name", "yoopost-bot")
    git(tree, "config", "user.email", "yoopost-bot@users.noreply.github.com")
    if not (git(tree, "add", "--", str(path.relative_to(tree))) and
            git(tree, "commit", "-q", "-m",
                f"request: {record['id']} {'실행 실패' if record.get('fire_error') else '접수'} ({record['kind']})")):
        raise RuntimeError("요청 기록을 커밋하지 못했습니다")
    for attempt in range(1, PUSH_TRIES + 1):
        if git(tree, "pull", "--rebase", "-q", "origin", BRANCH) and git(tree, "push", "-q", "origin", f"HEAD:{BRANCH}"):
            return
        git(tree, "rebase", "--abort")
        time.sleep(3 * attempt)
    raise RuntimeError("요청 기록을 원고 브랜치에 올리지 못했습니다 (git push 실패)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="슬랙 수동 지시 하나를 처리 (GitHub Actions manual_request.yml 전용 — 입력은 환경 변수)")
    parser.add_argument("--tree", type=Path, default=ROOT / WORKTREE, help="yoopost-drafts 브랜치를 받은 폴더")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    manual_channel = env("SLACK_CHANNEL_MANUAL")
    token, url, bot = env("ROUTINE_FIRE_TOKEN"), env("ROUTINE_FIRE_URL"), env("SLACK_BOT_TOKEN")
    if not manual_channel or not bot:
        print("::error::SLACK_CHANNEL_MANUAL(변수) 또는 SLACK_BOT_TOKEN(비밀값)이 없습니다 — "
              "python scripts/setup_slack_channels.py 로 채널을 만들어 주세요")
        return 1
    notifier = SlackNotifier(bot_token=bot, channel_id=manual_channel, secrets=(token, bot))
    fire = (lambda text: fire_routine(url, token, text)) if token and url else None
    status = handle_request(
        os.environ.get("MANUAL_TEXT", ""), env("MANUAL_CHANNEL"), env("MANUAL_TS"), env("MANUAL_USER"),
        env("MANUAL_SOURCE") or "message", manual_channel=manual_channel, notifier=notifier, fire=fire,
        record=lambda record: record_to_branch(args.tree.resolve(), record),
        alert_channel=env("SLACK_CHANNEL_ID"),  # #yoopost-알림 진행 카드 (비우면 올리지 않음)
        fallback=dispatch_codex,
    )
    print(f"수동 지시 처리: {status}")
    if status in ("failed", "not configured"):
        print(f"::warning::수동 지시를 루틴에 넘기지 못했습니다 ({status}) — 슬랙 스레드에 이유를 남겼습니다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
