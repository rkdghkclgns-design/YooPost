"""비밀값 하나를 입력 창에 붙여넣어 .env 와 GitHub 시크릿에 함께 저장 (키 교체용).

실행
  python scripts/set_secret.py GEMINI_API_KEY

값은 화면에 * 로만 보이고, 터미널·로그에 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autopost.local_secrets import save_everywhere  # noqa: E402
from autopost.setup_browser import tk_prompt  # noqa: E402

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
HINTS = {
    "GEMINI_API_KEY": "https://aistudio.google.com/apikey 에서 만든 API 키를 붙여넣으세요",
    "GH_SECRETS_TOKEN": "GitHub 에서 방금 만든 토큰(github_pat_ 로 시작)을 붙여넣으세요\n"
                        "— 네이버 로그인 정보가 갱신되면 GitHub 에 자동 저장하는 데 씁니다",
}


def ask_value(name: str, prompt: Callable[[str, str, bool], str] = tk_prompt) -> str:
    hint = HINTS.get(name, f"{name} 값을 붙여넣으세요")
    return prompt(f"{name} 설정", f"{hint}\n(화면에는 * 로 표시됩니다)", True).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="비밀값을 .env 와 GitHub 시크릿에 저장")
    parser.add_argument("name", help="예: GEMINI_API_KEY")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--no-upload", action="store_true", help="GitHub 시크릿 등록을 건너뜀")
    args = parser.parse_args()

    if not NAME_RE.match(args.name):
        print("❌ 이름은 영문 대문자·숫자·밑줄만 쓸 수 있습니다 (예: GEMINI_API_KEY)")
        return 1
    value = ask_value(args.name)
    if not value:
        print("❌ 값이 입력되지 않았습니다.")
        return 1
    failed = save_everywhere({args.name: value}, Path(args.env), upload=not args.no_upload)
    print(f"✅ {args.env} 에 저장: {args.name}")
    if failed:
        print(f"⚠️ GitHub 시크릿 등록 실패: {args.name} — gh auth status 를 확인하세요.")
        return 1
    if not args.no_upload:
        print(f"✅ GitHub 시크릿 등록 완료: {args.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
