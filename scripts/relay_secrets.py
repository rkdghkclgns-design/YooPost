"""슬랙 명령 중계(Cloudflare Worker) 비밀값 등록 — 메모장에 붙여넣은 값을 wrangler 로 보내고 파일을 지운다.

    python scripts/relay_secrets.py --open   # 붙여넣을 메모장 2개 열기 (SLACK_SIGNING_SECRET, GITHUB_TOKEN)
    python scripts/relay_secrets.py          # 저장한 뒤 실행 → Worker 에 등록, 파일 삭제

- SLACK_SIGNING_SECRET : 슬랙 앱 > Basic Information > App Credentials > Signing Secret
- GITHUB_TOKEN         : GitHub fine-grained 토큰 (YooPost 리포지토리만, Actions: Read and write 하나)
값은 wrangler 의 표준입력으로만 전달하고 화면·명령줄에 남기지 않는다. (.env·GitHub 에는 저장하지 않음 — Worker 전용)
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RELAY_DIR = ROOT / "slack_relay"
SECRETS = {
    "SLACK_SIGNING_SECRET": "슬랙 앱 Basic Information 의 Signing Secret",
    "GITHUB_TOKEN": "GitHub fine-grained 토큰 (github_pat_ 로 시작)",
}


def paste_file(name: str) -> Path:
    return ROOT / "output" / f"paste_{name}.txt"


def read_value(path: Path) -> str:
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip().strip("'\"")
        if text and not text.startswith("#"):
            return text
    return ""


def open_files() -> None:
    for name, hint in SECRETS.items():
        path = paste_file(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# 아래 빈 줄에 {hint} 을(를) 붙여넣고 Ctrl+S 로 저장하세요.\n\n", encoding="utf-8")
        subprocess.Popen(["notepad.exe", str(path)])  # noqa: S603,S607 - 로컬 메모장
        print(f"메모장 열림: {path.name}")


def put_secret(name: str, value: str, run=subprocess.run) -> bool:
    npx = shutil.which("npx") or "npx"
    # wrangler 출력은 UTF-8 — 윈도우 기본(cp949)으로 읽으면 디코딩 오류가 난다
    result = run([npx, "wrangler", "secret", "put", name], input=value, text=True, encoding="utf-8",
                 errors="replace", capture_output=True, cwd=str(RELAY_DIR))
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="슬랙 중계 Worker 비밀값 등록")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    if args.open:
        open_files()
        return 0
    failed = []
    for name in SECRETS:
        path = paste_file(name)
        value = read_value(path) if path.is_file() else ""
        if not value:
            print(f"⏭️ {name}: 붙여넣은 값이 없어 건너뜁니다")
            failed.append(name)
            continue
        if put_secret(name, value):
            path.unlink()
            print(f"✅ Worker 비밀값 등록: {name} (길이 {len(value)}자) — 붙여넣은 파일은 지웠습니다")
        else:
            print(f"❌ Worker 비밀값 등록 실패: {name} — npx wrangler whoami 로 로그인 상태를 확인하세요")
            failed.append(name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
