"""오늘 원고 진행 상황을 슬랙 #yoopost-알림 메시지 1개로 보여 주고 갱신한다 (진행 막대 ▰▰▱ + 카테고리별 상태).

    python scripts/draft_progress.py                          # 지금 상태로 올리기/고치기
    python scripts/draft_progress.py --skip "AI 공모주 분석" --reason "최근 공모 없음"
    python scripts/draft_progress.py --watch                  # 7개 카테고리가 다 끝날 때까지 1분마다 갱신 (최대 4시간)
    python scripts/draft_progress.py --watch --until-published  # 승인·발행까지 계속 지켜보기 (최대 12시간)

AI 호출 없음. 필요: SLACK_BOT_TOKEN·SLACK_CHANNEL_ID (메시지 수정은 봇 토큰이 있어야 함), IMAGE_HOST_URL·IMAGE_UPLOAD_TOKEN(발행 여부 표시).
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from autopost.config import load_config  # noqa: E402
from autopost.draft_progress import ProgressBoard  # noqa: E402
from autopost.draft_store import DraftStore  # noqa: E402
from autopost.pipeline import KST  # noqa: E402
from autopost.slack_helper import SlackNotifier  # noqa: E402

DRAFTS = ROOT / "output" / "claude_drafts"


def main() -> int:
    parser = argparse.ArgumentParser(description="오늘 원고 진행 상황 (슬랙)")
    parser.add_argument("--skip", default="", help="건너뛴 카테고리")
    parser.add_argument("--reason", default="쓸 만한 새 소식 없음")
    parser.add_argument("--watch", action="store_true", help="끝날 때까지 1분마다 갱신")
    parser.add_argument("--until-published", action="store_true", help="모든 원고가 발행될 때까지 지켜보기")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    cfg = load_config()
    slack = SlackNotifier(cfg.slack_bot_token, cfg.slack_channel_id, cfg.slack_webhook_url, secrets=cfg.secret_values())
    store = DraftStore(cfg.image_host_url, cfg.image_upload_token) if cfg.image_host_enabled else None
    now = lambda: datetime.now(KST)  # noqa: E731
    board = ProgressBoard(slack, DRAFTS, DRAFTS / f"progress_{now():%Y%m%d}.json", store, now=now)
    if args.skip:
        board.skip(args.skip, args.reason)
    deadline = time.time() + (12 if args.until_published else 4) * 3600
    while True:
        rows = board.refresh()
        done = sum(1 for r in rows if r.finished)
        published = all(r.icon in ("✅", "⏭️") for r in rows)
        print(f"{now():%H:%M} 진행 {done}/{len(rows)} " + " ".join(r.icon for r in rows), flush=True)
        finished = published if args.until_published else done == len(rows)
        if not args.watch or finished or time.time() > deadline:
            return 0
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
