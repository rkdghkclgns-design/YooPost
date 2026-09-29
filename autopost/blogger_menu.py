"""구글 블로그 상단 메뉴 — 홈 | AI 언어 모델 | AI 도구 | … | AI 공모주 분석.

글마다 카테고리를 첫 번째 라벨로 붙이므로(blog_publisher.build_labels), 메뉴 항목은 라벨 페이지로 연결하면 된다.
Blogger API 로는 레이아웃(가젯)을 바꿀 수 없어, 메뉴는 '레이아웃 → 페이지 가젯 → 외부 링크 추가'로 한 번 등록한다.
"""
from __future__ import annotations

from urllib.parse import quote

from .constants import CATEGORIES, HOME_MENU


def label_url(blog_url: str, label: str) -> str:
    return f"{blog_url.rstrip('/')}/search/label/{quote(label)}"


def menu_links(blog_url: str) -> list[tuple[str, str]]:
    """(메뉴 이름, 주소) — 첨부 이미지와 같은 순서."""
    home = blog_url.rstrip("/") + "/"
    return [(HOME_MENU, home), *((category, label_url(blog_url, category)) for category in CATEGORIES)]
