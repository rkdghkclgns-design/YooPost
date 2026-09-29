"""슬랙 연결 테스트 — .env 의 봇(SLACK_BOT_TOKEN/SLACK_CHANNEL_ID)과 웹훅(SLACK_WEBHOOK_URL)을 각각 확인한다.

실행
  python scripts/slack_test.py

토큰 값은 화면에 출력하지 않는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from slack_sdk import WebClient  # noqa: E402
from slack_sdk.errors import SlackApiError  # noqa: E402
from slack_sdk.webhook import WebhookClient  # noqa: E402

from autopost.config import load_config  # noqa: E402

HINTS = {
    "invalid_auth": "봇 토큰이 잘못되었습니다. xoxb- 로 시작하는 'Bot User OAuth Token' 전체를 붙여넣었는지 확인하세요.",
    "not_authed": "SLACK_BOT_TOKEN 이 비어 있습니다.",
    "channel_not_found": "SLACK_CHANNEL_ID 가 잘못되었거나 봇이 볼 수 없는 채널입니다.",
    "not_in_channel": "봇이 채널 멤버가 아닙니다. 채널에서 /invite @yoopost 를 실행하세요.",
}


def check_bot(token: str, channel: str) -> bool:
    if not token.startswith("xoxb-"):
        print("❌ 봇: .env 의 SLACK_BOT_TOKEN 에 xoxb- 로 시작하는 봇 토큰을 붙여넣으세요.")
        return False
    if not channel:
        print("❌ 봇: .env 의 SLACK_CHANNEL_ID 가 비어 있습니다.")
        return False
    client = WebClient(token=token)
    try:
        auth = client.auth_test()
        client.chat_postMessage(channel=channel, text="✅ [YooPost] 봇 연결 테스트 성공!")
    except SlackApiError as exc:
        code = exc.response.get("error", "unknown")
        print(f"❌ 봇 오류: {code}\n   {HINTS.get(code, '')}")
        return False
    print(f"✅ 봇 연결 성공 — 워크스페이스: {auth.get('team')}, 봇: {auth.get('user')}")
    return True


def check_webhook(url: str) -> bool:
    if not url.startswith("https://hooks.slack.com/"):
        print("❌ 웹훅: SLACK_WEBHOOK_URL 은 https://hooks.slack.com/ 으로 시작해야 합니다.")
        return False
    response = WebhookClient(url).send(text="✅ [YooPost] 웹훅 연결 테스트 성공! (봇 전송 실패 시 예비 경로)")
    if response.status_code != 200:
        print(f"❌ 웹훅 오류: HTTP {response.status_code} {response.body}")
        return False
    print("✅ 웹훅 연결 성공")
    return True


def main() -> int:
    load_dotenv()
    cfg = load_config()
    results = []
    if cfg.slack_bot_token or cfg.slack_channel_id:
        results.append(check_bot(cfg.slack_bot_token, cfg.slack_channel_id))
    if cfg.slack_webhook_url:
        results.append(check_webhook(cfg.slack_webhook_url))
    if not results:
        print("❌ .env 에 SLACK_BOT_TOKEN+SLACK_CHANNEL_ID 또는 SLACK_WEBHOOK_URL 을 넣으세요.")
        return 1
    print("   슬랙 채널에서 테스트 메시지를 확인하세요.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
