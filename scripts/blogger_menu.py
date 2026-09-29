"""구글 블로그 'AI백과사전' 상단 메뉴(섹션)에 넣을 링크 목록 출력.

    python scripts/blogger_menu.py                                  # .env 의 구글 토큰으로 블로그 주소 확인
    python scripts/blogger_menu.py --blog-url https://xxx.blogspot.com

메뉴: 홈 | AI 언어 모델 | AI 도구 | AI 논문 분석 | AI 뉴스 리뷰 | AI 보안 | AI 인사이트 | AI 공모주 분석
등록: Blogger → 레이아웃 → (헤더 아래) '가젯 추가' → '페이지' → '홈' 체크 + '외부 링크 추가'로 아래 7개를 순서대로
      → 저장. (테마에 '페이지' 가젯이 이미 있으면 그 가젯을 편집)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autopost.blogger_menu import menu_links  # noqa: E402


def blog_url_from_api() -> str:
    from dotenv import load_dotenv

    from autopost.config import load_config
    from autopost.google_auth import build_blogger_service, build_credentials

    load_dotenv()
    cfg = load_config()
    if cfg.google is None or not cfg.blogger_blog_id:
        raise SystemExit("❌ .env 에 구글 토큰·BLOGGER_BLOG_ID 가 없습니다. scripts/get_google_token.py 를 먼저 실행하거나 --blog-url 을 주세요.")
    service = build_blogger_service(build_credentials(cfg.google))
    return service.blogs().get(blogId=cfg.blogger_blog_id).execute()["url"]


def main() -> int:
    parser = argparse.ArgumentParser(description="구글 블로그 상단 메뉴 링크 목록")
    parser.add_argument("--blog-url", default="", help="블로그 주소 (비우면 Blogger API 로 확인)")
    args = parser.parse_args()
    blog_url = args.blog_url or blog_url_from_api()
    print(f"블로그: {blog_url}\n")
    for index, (name, url) in enumerate(menu_links(blog_url)):
        print(f"{index}. {name:<10} {url}")
    print("\nBlogger → 레이아웃 → '페이지' 가젯 → '홈' 체크 + 나머지는 '외부 링크 추가'로 위 순서대로 등록 → 저장")
    return 0


if __name__ == "__main__":
    sys.exit(main())
