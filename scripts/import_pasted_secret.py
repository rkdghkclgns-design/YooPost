"""메모장에 붙여넣은 비밀값을 .env 와 GitHub 시크릿으로 옮기고 파일을 지운다 (값은 화면에 출력하지 않음).

    python scripts/import_pasted_secret.py SLACK_BOT_TOKEN --open   # output/paste_SLACK_BOT_TOKEN.txt 를 메모장으로 열기
    python scripts/import_pasted_secret.py SLACK_BOT_TOKEN          # 붙여넣고 저장한 뒤 실행 → 옮기고 파일 삭제

set_secret.py 의 입력 창이 화면에 뜨지 않는 환경(원격·백그라운드 실행)에서 쓰는 대안이다.
output/ 폴더는 git 에 올라가지 않는다.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autopost.local_secrets import save_everywhere  # noqa: E402

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
# 이름별 허용 시작 문자 — Gemini 키는 예전 형식(AIza…)과 새 형식(AQ.…) 모두 있다
PREFIXES = {
    "SLACK_BOT_TOKEN": ("xoxb-",),
    "SLACK_WEBHOOK_URL": ("https://hooks.slack.com/",),
    "GEMINI_API_KEY": ("AIza", "AQ."),
    "ROUTINE_FIRE_TOKEN": ("sk-ant-",),
}


def paste_file(name: str) -> Path:
    return ROOT / "output" / f"paste_{name}.txt"


def read_value(path: Path) -> str:
    """주석(#)·빈 줄을 뺀 첫 줄 — 앞뒤 공백·따옴표 제거."""
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip().strip("'\"")
        if text and not text.startswith("#"):
            return text
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="붙여넣은 비밀값을 .env·GitHub 로 옮기기")
    parser.add_argument("name")
    parser.add_argument("--open", action="store_true", help="붙여넣을 파일을 메모장으로 열기")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args()
    if not NAME_RE.match(args.name):
        print("❌ 이름은 영문 대문자·숫자·밑줄만 (예: SLACK_BOT_TOKEN)")
        return 1
    path = paste_file(args.name)

    if args.open:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# 아래 빈 줄에 {args.name} 값을 붙여넣고 Ctrl+S 로 저장한 뒤 창을 닫으세요.\n\n",
                        encoding="utf-8")
        subprocess.Popen(["notepad.exe", str(path)])  # noqa: S603,S607 - 로컬 메모장
        print(f"메모장을 열었습니다: {path.name}")
        return 0

    if not path.is_file():
        print(f"❌ {path.name} 파일이 없습니다. --open 으로 먼저 여세요.")
        return 1
    value = read_value(path)
    if not value:
        print("❌ 값이 비어 있습니다. 메모장에 붙여넣고 Ctrl+S 로 저장했는지 확인하세요.")
        return 1
    prefixes = PREFIXES.get(args.name, ())
    if prefixes and not value.startswith(prefixes):
        print(f"❌ {args.name} 값은 {' 또는 '.join(prefixes)} 로 시작해야 합니다. "
              "다른 값을 붙여넣은 것 같습니다 (파일은 그대로 둡니다).")
        return 1
    failed = save_everywhere({args.name: value}, Path(args.env), upload=not args.no_upload)
    path.unlink()
    print(f"✅ .env 에 저장했습니다: {args.name} (길이 {len(value)}자) — 붙여넣은 파일은 지웠습니다")
    if failed:
        print(f"⚠️ GitHub 시크릿 등록 실패: {args.name} — gh auth status 를 확인하세요")
        return 1
    if not args.no_upload:
        print(f"✅ GitHub 시크릿 등록 완료: {args.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
