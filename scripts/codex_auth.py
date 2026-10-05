"""클라우드 전용 Codex 로그인 복원·암호화 보존. 인증 내용을 출력하지 않는다."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def restore() -> None:
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        done = subprocess.run(["codex", "login", "--with-api-key"], input=key, text=True, capture_output=True)
        if done.returncode:
            raise RuntimeError("OpenAI API 인증 설정 실패")
        return
    from cryptography.fernet import Fernet

    cipher = Fernet(os.environ["CODEX_AUTH_CACHE_KEY"].encode("ascii"))
    cache = Path(os.environ["CODEX_AUTH_CACHE_FILE"])
    raw = cipher.decrypt(cache.read_bytes()).decode("utf-8") if cache.is_file() else os.environ["CODEX_AUTH_JSON"]
    auth = json.loads(raw)
    if auth.get("auth_mode") != "chatgpt" or not (auth.get("tokens") or {}).get("refresh_token"):
        raise ValueError("클라우드 전용 ChatGPT 로그인이 필요합니다")
    path = home / "auth.json"
    path.write_text(json.dumps(auth), encoding="utf-8")
    path.chmod(0o600)
    print("클라우드 전용 Codex 인증 복원 완료")


def persist() -> None:
    path = Path(os.environ["CODEX_HOME"]) / "auth.json"
    if not path.is_file() or not os.environ.get("CODEX_AUTH_CACHE_KEY"):
        return
    auth = json.loads(path.read_text(encoding="utf-8"))
    if auth.get("auth_mode") != "chatgpt":
        return
    from cryptography.fernet import Fernet

    cache = Path(os.environ["CODEX_AUTH_CACHE_FILE"])
    cache.write_bytes(Fernet(os.environ["CODEX_AUTH_CACHE_KEY"].encode("ascii")).encrypt(path.read_bytes()))
    cache.chmod(0o600)
    print("갱신된 Codex 인증을 암호화해 보존했습니다")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("restore", "persist"))
    args = parser.parse_args()
    {"restore": restore, "persist": persist}[args.action]()
