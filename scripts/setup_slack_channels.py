"""YooPost 슬랙 채널을 지금 작업 흐름(Claude 작성 → ✅ 승인 → 발행)에 맞게 만들고·이름을 바꾸고·정리한다.

    python scripts/setup_slack_channels.py                 # GOOGLE_ACCOUNT_EMAIL(기본 rkdghkclgns@gmail.com) 초대
    python scripts/setup_slack_channels.py --email me@x.com

    #yoopost-알림        매일 원고 작성 요약 · 오류 · 연동 점검              (SLACK_CHANNEL_ID, Secret)
    #yoopost-1-원고승인   Claude 가 쓴 원고 — 미리보기·출처 확인 후 ✅ = 발행   (SLACK_CHANNEL_DRAFT)
    #yoopost-2-발행완료   ✅ 로 발행된 글 주소                                (SLACK_CHANNEL_PUBLISH)
예전 이름(#yoopost-2-원고검증, #yoopost-3-발행결과)은 채널을 그대로 두고 이름만 바꾸며(채널 ID 유지),
더 이상 쓰지 않는 #yoopost-1-주제조사 는 보관(archive)한다. 여러 번 실행해도 안전하다.
필요: .env 의 SLACK_BOT_TOKEN (앱에 channels:manage, channels:read, users:read.email 권한). 토큰 값은 출력하지 않는다.
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

# (채널 이름, 예전 이름들, .env 이름, 비밀 여부, 채널 설명)
CHANNELS = (
    ("yoopost-알림", (), "SLACK_CHANNEL_ID", True,
     "AI백과사전 — 매일 원고 작성 요약 · 오류 · 연동 점검(/aipost-setup)"),
    ("yoopost-1-원고승인", ("yoopost-2-원고검증",), "SLACK_CHANNEL_DRAFT", False,
     "Claude 가 쓴 원고 — 스레드의 미리보기·출처를 확인하고 ✅ 를 누르면 구글 블로그에 발행"),
    ("yoopost-2-발행완료", ("yoopost-3-발행결과",), "SLACK_CHANNEL_PUBLISH", False,
     "✅ 승인으로 구글 블로그에 발행된 글 주소"),
)
RETIRED = ("yoopost-1-주제조사",)  # 더 이상 쓰지 않는 채널 — 보관
RETIRED_ENV = ("SLACK_CHANNEL_RESEARCH",)
GUIDE = ("📌 *이 채널 사용법*\n"
         "Claude 가 매일 아침 카테고리별 원고를 여기에 올립니다. 스레드의 미리보기(HTML)·출처를 확인하고\n"
         "원고 메시지에 ✅ 를 누르면 구글 블로그에 발행되고, 결과가 그 스레드에 달립니다. 발행하지 않을 원고는 그냥 두면 됩니다.")


def all_channels(client) -> dict[str, dict]:
    found: dict[str, dict] = {}
    cursor = None
    while True:
        page = client.conversations_list(types="public_channel", limit=200, cursor=cursor, exclude_archived=False)
        found.update({c["name"]: c for c in page["channels"]})
        cursor = page.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return found


def _try(action: str, call) -> bool:
    """권한이 없어도 전체 정리를 멈추지 않는다 (경고만)."""
    from slack_sdk.errors import SlackApiError

    try:
        call()
        return True
    except SlackApiError as exc:
        print(f"   ⚠️ {action} 건너뜀: {exc.response.get('error')}")
        return False


def _join(client, channel: dict) -> None:
    """봇이 이미 멤버면 참여 요청을 하지 않는다 (channels:join 권한이 없어도 되도록)."""
    if not channel.get("is_member"):
        _try(f"#{channel.get('name', '')} 참여", lambda: client.conversations_join(channel=channel["id"]))


def ensure_channel(client, known: dict[str, dict], name: str, old_names, purpose: str) -> tuple[str, bool]:
    """(채널 ID, 새로 만들었거나 이름을 바꿨는지)."""
    live = {n: c for n, c in known.items() if not c.get("is_archived")}
    if name in live:
        channel_id, changed = live[name]["id"], False
        _join(client, live[name])
    elif any(old in live for old in old_names):
        old = next(o for o in old_names if o in live)
        channel_id = live[old]["id"]
        _join(client, live[old])
        client.conversations_rename(channel=channel_id, name=name)
        print(f"✏️ 이름 변경: #{old} → #{name}")
        changed = True
    else:
        channel_id = client.conversations_create(name=name)["channel"]["id"]
        print(f"✅ 채널 생성: #{name}")
        changed = True
    _try(f"#{name} 설명", lambda: client.conversations_setPurpose(channel=channel_id, purpose=purpose))
    return channel_id, changed


def retire(client, known: dict[str, dict]) -> None:
    for name in RETIRED:
        channel = known.get(name)
        if channel and not channel.get("is_archived"):
            _join(client, channel)
            if _try(f"#{name} 보관", lambda: client.conversations_archive(channel=channel["id"])):
                print(f"📦 보관: #{name} (더 이상 쓰지 않음 — 출처는 원고 스레드에 올라감)")


def invite(client, channel_id: str, user_id: str | None, name: str) -> None:
    from slack_sdk.errors import SlackApiError

    if not user_id:
        return
    try:
        client.conversations_invite(channel=channel_id, users=user_id)
    except SlackApiError as exc:
        if exc.response.get("error") not in ("already_in_channel", "cant_invite_self"):
            print(f"   (초대 실패 #{name}: {exc.response.get('error')})")


def main() -> int:
    parser = argparse.ArgumentParser(description="YooPost 슬랙 채널 정리")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--email", default="")
    args = parser.parse_args()
    load_dotenv(args.env)
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token.startswith("xoxb-"):
        print("❌ .env 에 SLACK_BOT_TOKEN(xoxb-...) 이 없습니다")
        return 1

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    client = WebClient(token=token)
    email = args.email or os.environ.get("GOOGLE_ACCOUNT_EMAIL", "rkdghkclgns@gmail.com")
    ids: dict[str, str] = {}
    try:
        team = client.auth_test()["team"]
        try:
            user_id = client.users_lookupByEmail(email=email)["user"]["id"]
        except SlackApiError:
            user_id = None
            print(f"⚠️ {email} 사용자를 찾지 못해 초대는 건너뜁니다")
        known = all_channels(client)
        for name, old_names, env, _, purpose in CHANNELS:
            channel_id, changed = ensure_channel(client, known, name, old_names, purpose)
            invite(client, channel_id, user_id, name)
            if changed and env == "SLACK_CHANNEL_DRAFT":
                client.chat_postMessage(channel=channel_id, text=GUIDE)
            ids[env] = channel_id
        retire(client, known)
    except SlackApiError as exc:
        print(f"❌ 슬랙 오류: {exc.response.get('error')} — 앱 권한(channels:manage 등)을 확인하세요")
        return 1

    update_env_file(Path(args.env), {**ids, **{env: "" for env in RETIRED_ENV}})
    secret_names = {env for _, _, env, secret, _ in CHANNELS if secret}
    failed = upload_secrets({k: v for k, v in ids.items() if k in secret_names})
    failed += upload_variables({k: v for k, v in ids.items() if k not in secret_names})
    print(f"\n워크스페이스 {team}: 채널 ID 를 {args.env} 에 저장했습니다.")
    print("GitHub 등록 실패: " + ", ".join(failed) if failed else "✅ GitHub Secrets/Variables 등록 완료")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
