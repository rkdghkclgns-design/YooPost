"""구글 OAuth 사용자 자격증명 (구글 블로그 전용 — 구글 드라이브 권한은 쓰지 않는다).

서비스 계정은 Blogger 글 작성이 불가하므로 사용자 계정의 refresh token 방식을 사용한다.
예전에 드라이브 권한까지 받은 토큰도 그대로 쓸 수 있다 (갱신할 때 블로그 권한만 요청).
"""
from __future__ import annotations

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import GoogleConfig

SCOPES = ("https://www.googleapis.com/auth/blogger",)
TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleAuthError(RuntimeError):
    """refresh token 만료/폐기 등 인증 실패."""


def build_credentials(google: GoogleConfig) -> Credentials:
    creds = Credentials(
        token=None,
        refresh_token=google.refresh_token,
        token_uri=TOKEN_URI,
        client_id=google.client_id,
        client_secret=google.client_secret,
        scopes=list(SCOPES),
    )
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        raise GoogleAuthError(
            "구글 refresh token 이 만료되었거나 폐기되었습니다. "
            "scripts/get_google_token.py 를 다시 실행해 GOOGLE_REFRESH_TOKEN 을 갱신하세요. "
            "(OAuth 동의화면이 '테스트' 상태면 7일마다 만료되므로 '프로덕션'으로 게시하세요)"
        ) from exc
    return creds


def build_blogger_service(creds: Credentials):
    return build("blogger", "v3", credentials=creds, cache_discovery=False)
