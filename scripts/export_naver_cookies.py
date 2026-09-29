"""네이버 로그인 세션 저장 (내 PC에서 1회 실행).

실행
  python -m playwright install chromium     # 최초 1회
  python scripts/export_naver_cookies.py

1. 크롬 창이 열리면 네이버에 '직접' 로그인하세요 ('로그인 상태 유지' 체크 권장).
   (보안문자·2단계 인증도 직접 처리합니다. 이 스크립트는 비밀번호를 다루지 않습니다)
2. 로그인이 끝나면 자동으로 감지합니다 (최대 10분 대기, Enter 불필요).
3. 블로그 아이디를 자동으로 찾아 NAVER_BLOG_ID / NAVER_STORAGE_STATE_B64 를
   .env 와 GitHub 시크릿(gh CLI)에 바로 저장합니다. 값은 화면에 출력하지 않습니다.
   (세션 값이 있으면 누구나 내 네이버 계정으로 로그인할 수 있으니 공유하지 마세요)

주의: 네이버 '해외 로그인 차단'이 켜져 있으면 GitHub Actions(해외 서버)에서 세션이 막힐 수 있습니다.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autopost.local_secrets import save_everywhere  # noqa: E402
from autopost.naver_publisher import encode_storage_state  # noqa: E402
from autopost.setup_browser import launch_visible_browser  # noqa: E402

LOGIN_URL = "https://nid.naver.com/nidlogin.login"
MY_BLOG_URL = "https://blog.naver.com/MyBlog.naver"  # 로그인 상태면 내 블로그 주소로 이동
REQUIRED_COOKIES = {"NID_AUT", "NID_SES"}
BLOG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,30}$")
BLOG_HOSTS = {"blog.naver.com", "m.blog.naver.com"}
NOT_BLOG_IDS = {"prologue", "section", "gonaverblog"}


def blog_id_from_url(url: str) -> str | None:
    """blog.naver.com/<아이디> 또는 ...?blogId=<아이디> 에서 블로그 아이디를 꺼낸다."""
    parsed = urlparse(url)
    if (parsed.hostname or "") not in BLOG_HOSTS:
        return None
    candidate = parse_qs(parsed.query).get("blogId", [""])[0]
    if not candidate:
        segment = parsed.path.strip("/").split("/")[0]
        candidate = "" if "." in segment else segment  # MyBlog.naver 같은 페이지 이름 제외
    if BLOG_ID_RE.match(candidate) and candidate.lower() not in NOT_BLOG_IDS:
        return candidate
    return None


def ask_blog_id() -> str:
    while True:
        typed = input("블로그 주소 blog.naver.com/ 뒤의 아이디를 입력하세요: ").strip()
        if BLOG_ID_RE.match(typed):
            return typed
        print("  영문·숫자·_·- 3~30자로 입력해주세요.")


def has_login_cookies(cookies) -> bool:
    return REQUIRED_COOKIES <= {cookie.get("name") for cookie in cookies}


def is_persistent_login(cookies) -> bool:
    """'로그인 상태 유지'를 체크하면 NID_AUT 에 만료일이 붙는다 — 없으면 브라우저를 닫는 순간 끊기는 세션."""
    auth = next((c for c in cookies if c.get("name") == "NID_AUT"), None)
    return bool(auth) and (auth.get("expires") or -1) > 0


def wait_for_login(context, page, timeout_s: float, poll_ms: int = 2000) -> bool:
    """사용자가 창에서 로그인을 마칠 때까지(로그인 쿠키가 생길 때까지) 기다린다. 창을 닫으면 False."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        for _ in range(max(1, int(timeout_s * 1000 / poll_ms))):
            if has_login_cookies(context.cookies()):
                page.wait_for_timeout(3000)  # 로그인 직후 이동이 끝나도록 잠시 대기
                return True
            page.wait_for_timeout(poll_ms)
    except PlaywrightError:  # 로그인 전에 창을 닫음
        return False
    return False


def capture_session(timeout_s: float) -> tuple[dict, str | None]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = launch_visible_browser(playwright.chromium)
        context = browser.new_context(locale="ko-KR")
        page = context.new_page()
        page.goto(LOGIN_URL)
        print(f"\n열린 창에서 네이버에 로그인하세요 ('로그인 상태 유지' 체크). 최대 {int(timeout_s // 60)}분 기다립니다...")
        if not wait_for_login(context, page, timeout_s):
            browser.close()
            return {"cookies": []}, None
        blog_id = None
        try:
            page.goto(MY_BLOG_URL, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(1500)
            blog_id = blog_id_from_url(page.url)
        except Exception as exc:  # 블로그 확인 실패는 직접 입력으로 대체
            print(f"  (블로그 주소 자동 확인 실패: {exc.__class__.__name__})")
        state = context.storage_state()
        browser.close()
    return state, blog_id


def main() -> int:
    parser = argparse.ArgumentParser(description="네이버 로그인 세션 저장")
    parser.add_argument("--env", default=".env", help="값을 저장할 .env 경로 (기본: .env)")
    parser.add_argument("--no-upload", action="store_true", help="GitHub 시크릿 등록을 건너뜀")
    parser.add_argument("--blog-id", default="", help="블로그 아이디를 직접 지정 (자동 확인 실패 시)")
    parser.add_argument("--timeout", type=float, default=600, help="로그인 대기 시간(초), 기본 10분")
    args = parser.parse_args()

    state, found_id = capture_session(args.timeout)
    if not has_login_cookies(state.get("cookies", [])):
        print("❌ 로그인이 확인되지 않았습니다 (시간 초과 또는 창을 닫음). 다시 실행하세요.")
        return 1

    if not is_persistent_login(state.get("cookies", [])):
        print("❌ '로그인 상태 유지'가 체크되지 않아 곧 끊기는 로그인입니다 (자동 발행에 쓸 수 없음).")
        print("   다시 실행해서 로그인 화면의 '로그인 상태 유지'를 꼭 체크하고 로그인하세요.")
        return 1

    blog_id = args.blog_id or found_id
    if blog_id:
        print(f"✅ 블로그 아이디 확인: {blog_id}")
    elif sys.stdin.isatty():
        blog_id = ask_blog_id()
    else:
        print("❌ 블로그 아이디를 찾지 못했습니다. --blog-id 아이디 를 붙여 다시 실행하세요.")
        return 1

    values = {"NAVER_BLOG_ID": blog_id, "NAVER_STORAGE_STATE_B64": encode_storage_state(state)}
    failed = save_everywhere(values, Path(args.env), upload=not args.no_upload)
    print(f"✅ {args.env} 에 저장: {', '.join(values)}")
    if args.no_upload:
        return 0
    if failed:
        print(f"⚠️ GitHub 시크릿 등록 실패: {', '.join(failed)} — gh CLI 로그인(gh auth status)을 확인하세요.")
        return 1
    print("✅ GitHub 시크릿 등록 완료: " + ", ".join(values))
    return 0


if __name__ == "__main__":
    sys.exit(main())
