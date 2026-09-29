"""YooPost 슬랙 채널 5개를 만들고(있으면 그대로) 나를 초대한 뒤, 채널 ID 를 .env 와 GitHub 에 저장.

    python scripts/setup_slack_channels.py                 # GOOGLE_ACCOUNT_EMAIL(기본 rkdghkclgns@gmail.com) 초대
    python scripts/setup_slack_channels.py --email me@x.com

필요: .env 의 SLACK_BOT_TOKEN (앱에 channels:manage, channels:read, users:read.email 권한)
토큰 값은 화면에 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from autopost.envfile import update_env_file  # noqa: E402
from autopost.local_secrets import upload_secrets, upload_variables  # noqa: E402

# (채널 이름, .env 이름, 비밀 여부) — 알림 채널 ID 는 워크플로가 Secret 으로 읽는다
CHANNELS = (
    ("yoopost-알림", "SLACK_CHANNEL_ID", True),
    ("yoopost-1-주제조사", "SLACK_CHANNEL_RESEARCH", False),
    ("yoopost-2-원고검증", "SLACK_CHANNEL_DRAFT", False),
    ("yoopost-3-발행결과", "SLACK_CHANNEL_PUBLISH", False),
    ("yoopost-로그인토큰-만료알림", "SLACK_CHANNEL_TOKEN_ALERT", False),
)
PURPOSES = {
    "yoopost-알림": "AI백과사전 자동 포스팅 진행 상황·오류",
    "yoopost-1-주제조사": "고른 주제와 확인된 출처·사실 목록",
    "yoopost-2-원고검증": "AI 원고·사실 검증 보고·대표 이미지·블로그 미리보기",
    "yoopost-3-발행결과": "구글·네이버 발행 결과와 수동 발행용 원고",
    "yoopost-로그인토큰-만료알림": "네이버 로그인 만료 3일 전 알림",
}


def existing_channels(client) -> dict[str, str]:
    found: dict[str, str] = {}
    cursor = None
    while True:
        page = client.conversations_list(types="public_channel", limit=200, cursor=cursor, exclude_archived=True)
        found.update({c["name"]: c["id"] for c in page["channels"]})
        cursor = page.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return found


def ensure_channels(client, user_id: str | None) -> dict[str, str]:
    from slack_sdk.errors import SlackApiError

    known = existing_channels(client)
    ids: dict[str, str] = {}
    for name, _, _ in CHANNELS:
        channel_id = known.get(name)
        if channel_id is None:
            channel_id = client.conversations_create(name=name)["channel"]["id"]
            client.conversations_setPurpose(channel=channel_id, purpose=PURPOSES[name])
            print(f"✅ 채널 생성: #{name}")
        else:
            try:
                client.conversations_join(channel=channel_id)
            except SlackApiError:
                pass
            print(f"✅ 채널 있음: #{name}")
        if user_id:
            try:
                client.conversations_invite(channel=channel_id, users=user_id)
            except SlackApiError as exc:
                if exc.response.get("error") not in ("already_in_channel", "cant_invite_self"):
                    print(f"   (초대 실패 #{name}: {exc.response.get('error')})")
        ids[name] = channel_id
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description="YooPost 슬랙 채널 만들기")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--email", default="")
    args = parser.parse_args()
    load_dotenv(args.env)
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token.startswith("xoxb-"):
        print("❌ .env 에 SLACK_BOT_TOKEN(xoxb-...) 이 없습니다. python scripts/set_secret.py SLACK_BOT_TOKEN")
        return 1

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    client = WebClient(token=token)
    email = args.email or os.environ.get("GOOGLE_ACCOUNT_EMAIL", "rkdghkclgns@gmail.com")
    try:
        team = client.auth_test()["team"]
        try:
            user_id = client.users_lookupByEmail(email=email)["user"]["id"]
        except SlackApiError:
            user_id = None
            print(f"⚠️ {email} 사용자를 찾지 못해 초대는 건너뜁니다 (채널 목록에서 직접 참여하세요)")
        ids = ensure_channels(client, user_id)
    except SlackApiError as exc:
        print(f"❌ 슬랙 오류: {exc.response.get('error')} — 앱 권한(channels:manage 등)을 추가하고 재설치했는지 확인하세요")
        return 1

    values = {env: ids[name] for name, env, _ in CHANNELS}
    update_env_file(Path(args.env), values)
    secret_names = {env for _, env, secret in CHANNELS if secret}
    failed = upload_secrets({k: v for k, v in values.items() if k in secret_names})
    failed += upload_variables({k: v for k, v in values.items() if k not in secret_names})
    print(f"\n워크스페이스 {team}: 채널 ID 를 {args.env} 에 저장했습니다.")
    print("GitHub 등록 실패: " + ", ".join(failed) if failed else "✅ GitHub Secrets/Variables 등록 완료")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
