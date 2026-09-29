"""구글 OAuth refresh token 1회 발급 (내 PC에서 실행).

사전 준비 (README '1) 구글' 참고)
  1. rkdghkclgns@gmail.com 으로 https://www.blogger.com 에서 블로그 'AI백과사전' 만들기
  2. Google Cloud 콘솔에서 'Blogger API v3' 사용 설정 (구글 드라이브는 쓰지 않음)
  3. OAuth 동의 화면을 '프로덕션(게시됨)' 상태로 전환  ※ '테스트' 상태면 토큰이 7일 뒤 만료됨
  4. OAuth 클라이언트 ID(데스크톱 앱) JSON 을 client_secret.json 으로 저장

실행
  python scripts/get_google_token.py client_secret.json            # 결과를 .env 에 바로 저장 (화면 출력 없음)
  python scripts/get_google_token.py client_secret.json --print    # GitHub 시크릿 등록용으로 화면에 출력

브라우저가 열리면 GOOGLE_ACCOUNT_EMAIL(기본 rkdghkclgns@gmail.com) 계정이 미리 선택됩니다.
비밀번호는 브라우저에서 직접 입력하세요 (이 스크립트는 비밀번호를 다루지 않습니다).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autopost.constants import DEFAULT_BLOG_NAME  # noqa: E402
from autopost.envfile import update_env_file  # noqa: E402
from autopost.google_auth import SCOPES  # noqa: E402

DEFAULT_ACCOUNT = "rkdghkclgns@gmail.com"


def _client_info(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("installed") or data.get("web") or {}


def _list_blogs(creds) -> list[dict]:
    from googleapiclient.discovery import build

    try:
        blogger = build("blogger", "v3", credentials=creds, cache_discovery=False)
        return blogger.blogs().listByUser(userId="self").execute().get("items", [])
    except Exception as exc:
        print(f"\n(블로그 목록 조회 실패: {type(exc).__name__})")
        return []


def choose_blog(blogs: list[dict], wanted_name: str) -> str:
    """이름이 wanted_name 인 블로그 ID (없으면 블로그가 1개일 때 그 ID, 아니면 빈 값)."""
    if not blogs:
        print(f"\n⚠️ 이 계정에 Blogger 블로그가 없습니다. https://www.blogger.com 에서 '{wanted_name}' 블로그를 만든 뒤 다시 실행하세요.")
        return ""
    print("\n[구글 블로그 목록]")
    for blog in blogs:
        print(f"  {blog['id']}   {blog.get('name', '')} - {blog.get('url', '')}")
    named = [b for b in blogs if b.get("name", "").strip() == wanted_name]
    if named:
        print(f"  → '{wanted_name}' 블로그를 사용합니다.")
        return named[0]["id"]
    if len(blogs) == 1:
        print(f"  → '{wanted_name}' 이름의 블로그가 없어 유일한 블로그를 사용합니다. (블로그 설정에서 제목을 바꿀 수 있어요)")
        return blogs[0]["id"]
    print(f"  → '{wanted_name}' 블로그를 찾지 못했습니다. 사용할 ID 를 .env 의 BLOGGER_BLOG_ID 에 직접 넣으세요.")
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="구글 refresh token 발급")
    parser.add_argument("client_secret", nargs="?", default="client_secret.json")
    parser.add_argument("--env", default=".env", help="값을 저장할 .env 경로 (기본: .env)")
    parser.add_argument("--account", default=os.environ.get("GOOGLE_ACCOUNT_EMAIL", DEFAULT_ACCOUNT),
                        help="로그인 화면에 미리 선택할 구글 계정")
    parser.add_argument("--blog-name", default=os.environ.get("BLOG_NAME", DEFAULT_BLOG_NAME))
    parser.add_argument("--print", dest="print_values", action="store_true",
                        help=".env 대신 화면에 출력 (GitHub 시크릿 등록용)")
    args = parser.parse_args()

    path = Path(args.client_secret)
    if not path.is_file():
        print(f"❌ {path} 파일이 없습니다. Google Cloud 콘솔에서 OAuth 클라이언트(데스크톱 앱) JSON 을 받아 저장하세요.")
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(path), scopes=list(SCOPES))
    print(f"브라우저에서 {args.account} 계정으로 로그인·허용하세요.")
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent", login_hint=args.account)
    if not creds.refresh_token:
        print("❌ refresh token 이 발급되지 않았습니다. https://myaccount.google.com/permissions 에서 앱 권한을 삭제 후 다시 실행하세요.")
        return 1

    info = _client_info(path)
    values = {
        "GOOGLE_CLIENT_ID": info.get("client_id", ""),
        "GOOGLE_CLIENT_SECRET": info.get("client_secret", ""),
        "GOOGLE_REFRESH_TOKEN": creds.refresh_token,
    }
    blog_id = choose_blog(_list_blogs(creds), args.blog_name)
    if blog_id:
        values["BLOGGER_BLOG_ID"] = blog_id

    if args.print_values:
        print("\n✅ 발급 완료! 아래 값을 GitHub 시크릿에 등록하세요 (외부에 공유 금지)\n")
        for key, value in values.items():
            print(f"{key}={value}")
        return 0

    update_env_file(Path(args.env), values)
    print(f"\n✅ 발급 완료! {args.env} 에 저장했습니다: {', '.join(values)}")
    print("   (값은 화면에 표시하지 않았습니다. GitHub 시크릿 등록: python scripts/push_secrets.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
