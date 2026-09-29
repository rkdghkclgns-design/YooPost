"""로컬 .env 값을 GitHub 리포지토리 Secrets / Variables 로 한 번에 등록 (gh CLI 사용).

    python scripts/push_secrets.py            # 값이 있는 항목만 등록
    python scripts/push_secrets.py --dry-run  # 무엇을 등록할지 이름만 출력

- 비밀값(토큰·키)은 Secrets, 일반 설정은 Variables 로 나눠 등록한다.
- 값은 gh 의 표준입력으로만 전달해 화면·명령줄에 남기지 않는다.
- 현재 폴더가 GitHub 리포지토리에 연결되어 있어야 한다 (gh repo view 로 확인).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import dotenv_values  # noqa: E402

from autopost.local_secrets import upload_secrets, upload_variables  # noqa: E402

SECRETS = (
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "BLOGGER_BLOG_ID",
    "GEMINI_API_KEY", "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "SLACK_WEBHOOK_URL",
    "NAVER_BLOG_ID", "NAVER_STORAGE_STATE_B64", "GH_SECRETS_TOKEN",
)
VARIABLES = (
    "BLOG_NAME", "DRIVE_ROOT_FOLDER_NAME", "GEMINI_MODEL", "GEMINI_FALLBACK_MODELS", "NAVER_PUBLISH_MODE",
    "NAVER_ALIGN", "TOPIC_LOOKBACK_DAYS", "MIN_REFERENCES", "MAX_FIX_ROUNDS", "MIN_BODY_CHARS",
    "SLACK_CHANNEL_RESEARCH", "SLACK_CHANNEL_DRAFT", "SLACK_CHANNEL_PUBLISH", "SLACK_CHANNEL_TOKEN_ALERT",
    "AUTO_PUBLISH",
)


def split_values(env: dict) -> tuple[dict, dict]:
    def pick(names):
        return {n: str(env.get(n) or "").strip() for n in names if str(env.get(n) or "").strip()}
    return pick(SECRETS), pick(VARIABLES)


def main() -> int:
    parser = argparse.ArgumentParser(description=".env → GitHub Secrets/Variables")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    secrets, variables = split_values(dotenv_values(args.env))
    print(f"Secrets {len(secrets)}개: {', '.join(secrets) or '-'}")
    print(f"Variables {len(variables)}개: {', '.join(variables) or '-'}")
    if args.dry_run:
        return 0
    failed = upload_secrets(secrets) + upload_variables(variables)
    if failed:
        print(f"⚠️ 등록 실패: {', '.join(failed)} — gh auth status / gh repo view 를 확인하세요.")
        return 1
    print("✅ GitHub 등록 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
