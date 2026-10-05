"""클라우드 전용 로그인/API 키를 GitHub production 비밀값에 등록한다. 인증 값 출력 없음.

PC의 기본 Codex 로그인은 복사하지 않는다. 별도 CODEX_HOME으로 로그인한 파일만 지정한다.
등록 후 probe workflow로 확인하고 CODEX_FALLBACK_ENABLED를 true로 설정한다.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from pathlib import Path

REPO = "rkdghkclgns-design/YooPost"


def github_env() -> dict:
    env = os.environ.copy()
    if not env.get("GH_TOKEN"):
        done = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                              capture_output=True, text=True)
        auth = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
        env["GH_TOKEN"] = auth.get("password", "")
    if not env.get("GH_TOKEN"):
        raise RuntimeError("GitHub 인증이 필요합니다")
    return env


def gh(env: dict, args: list[str], data: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=data, env=env, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("GitHub 설정 실패 — production 비밀값·변수 쓰기 권한을 확인해 주세요")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--auth-file", type=Path)
    group.add_argument("--api-key-env", metavar="ENV_NAME")
    args = parser.parse_args()
    env = github_env()
    info = json.loads(gh(env, ["api", f"repos/{REPO}"]))
    if not info.get("private"):
        raise RuntimeError("전용 계정 인증은 비공개 저장소에서만 설정합니다")
    secrets: dict[str, str]
    if args.auth_file:
        active = Path.home() / ".codex/auth.json"
        if args.auth_file.resolve() == active.resolve():
            raise RuntimeError("PC의 기본 로그인을 공유할 수 없습니다. 클라우드 전용으로 로그인해 주세요")
        auth = json.loads(args.auth_file.read_text(encoding="utf-8-sig"))
        if auth.get("auth_mode") != "chatgpt" or not (auth.get("tokens") or {}).get("refresh_token"):
            raise RuntimeError("클라우드 전용 ChatGPT 로그인 파일이 필요합니다")
        from cryptography.fernet import Fernet
        secrets = {"CODEX_AUTH_JSON": json.dumps(auth), "CODEX_AUTH_CACHE_KEY": Fernet.generate_key().decode("ascii")}
    else:
        key = os.environ.get(args.api_key_env, "").strip()
        if not key:
            raise RuntimeError("지정한 환경 변수에 API 키가 없습니다")
        secrets = {"OPENAI_API_KEY": key}
    for name, value in secrets.items():
        gh(env, ["secret", "set", name, "--repo", REPO, "--env", "production"], value)
    gh(env, ["variable", "set", "CODEX_AUTH_REVISION", "--repo", REPO, "--env", "production"], uuid.uuid4().hex)
    gh(env, ["variable", "set", "CODEX_FALLBACK_ENABLED", "--repo", REPO], "false")
    print("클라우드 인증 등록 완료. probe workflow 실행 확인 후 자동 인계를 활성화하세요.")


if __name__ == "__main__":
    main()
