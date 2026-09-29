"""슬랙 #yoopost-알림 채널에 한 줄~여러 줄 알림을 보낸다 (매일 원고 작성 요약 등).

    python scripts/slack_notice.py "📚 오늘 원고 7편 중 6편 승인 대기 — #yoopost-1-원고승인 에서 ✅"
    python scripts/slack_notice.py --file output/claude_drafts/summary.md

토큰 값은 출력하지 않는다. 멘션(<!channel> 등)이 되지 않도록 이스케이프해서 보낸다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from autopost.config import load_config  # noqa: E402
from autopost.slack_helper import SlackNotifier, escape_mrkdwn  # noqa: E402

MAX_CHARS = 3500


def main() -> int:
    parser = argparse.ArgumentParser(description="슬랙 알림 채널에 메시지 보내기")
    parser.add_argument("text", nargs="?", default="")
    parser.add_argument("--file", type=Path)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    text = args.file.read_text(encoding="utf-8") if args.file else args.text
    if not text.strip():
        print("❌ 보낼 내용이 없습니다")
        return 2
    cfg = load_config()
    slack = SlackNotifier(cfg.slack_bot_token, cfg.slack_channel_id, cfg.slack_webhook_url, secrets=cfg.secret_values())
    if not slack.enabled:
        print("❌ SLACK_BOT_TOKEN·SLACK_CHANNEL_ID 또는 SLACK_WEBHOOK_URL 이 필요합니다")
        return 2
    slack.post(escape_mrkdwn(text.strip())[:MAX_CHARS])
    print("✅ 슬랙 알림 채널에 보냈습니다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
