"""블로그 이미지 저장소(Cloudflare Worker + KV) 업로드 비밀값을 만들어 세 곳에 한 번에 등록한다.

    python scripts/image_host_secret.py            # .env 에 값이 있으면 그대로, 없으면 새로 만들어 등록
    python scripts/image_host_secret.py --rotate   # 새 값으로 교체

- Worker 비밀값 IMAGE_UPLOAD_TOKEN (wrangler 표준입력)
- GitHub Secret IMAGE_UPLOAD_TOKEN, Variable IMAGE_HOST_URL (gh 표준입력)
- 로컬 .env (IMAGE_UPLOAD_TOKEN, IMAGE_HOST_URL)
값은 화면·명령줄에 남기지 않는다. 구글 드라이브 대신 이 저장소가 대표 이미지·인용 이미지를 공개 주소로 제공한다.
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import dotenv_values  # noqa: E402

from autopost.envfile import update_env_file  # noqa: E402
from autopost.local_secrets import upload_secrets, upload_variables  # noqa: E402
from relay_secrets import put_secret  # noqa: E402

DEFAULT_HOST = "https://yoopost-slack-relay.cleaning-autopost-slack-relay.workers.dev"
TOKEN_NAME = "IMAGE_UPLOAD_TOKEN"
HOST_NAME = "IMAGE_HOST_URL"


def main() -> int:
    parser = argparse.ArgumentParser(description="이미지 저장소 업로드 비밀값 등록")
    parser.add_argument("--rotate", action="store_true", help="새 값으로 교체")
    parser.add_argument("--host", default="", help=f"Worker 주소 (기본: .env 값 또는 {DEFAULT_HOST})")
    args = parser.parse_args()

    env_path = ROOT / ".env"
    current = dotenv_values(env_path) if env_path.is_file() else {}
    token = "" if args.rotate else str(current.get(TOKEN_NAME) or "").strip()
    token = token or secrets.token_urlsafe(32)
    host = (args.host or str(current.get(HOST_NAME) or "") or DEFAULT_HOST).rstrip("/")

    update_env_file(env_path, {TOKEN_NAME: token, HOST_NAME: host})
    print(f"✅ .env 저장: {TOKEN_NAME} (길이 {len(token)}자), {HOST_NAME}={host}")
    failed = []
    if put_secret(TOKEN_NAME, token):
        print(f"✅ Worker 비밀값 등록: {TOKEN_NAME}")
    else:
        failed.append(f"Worker {TOKEN_NAME}")
    failed += [f"GitHub {n}" for n in upload_secrets({TOKEN_NAME: token})]
    failed += [f"GitHub {n}" for n in upload_variables({HOST_NAME: host})]
    if failed:
        print(f"⚠️ 등록 실패: {', '.join(failed)} — npx wrangler whoami / gh auth status 를 확인하세요")
        return 1
    print(f"✅ GitHub Secret {TOKEN_NAME}, Variable {HOST_NAME} 등록 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
