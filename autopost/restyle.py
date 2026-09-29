"""이미 발행한 글·승인 대기 원고의 본문 스타일을 새 디자인(Hashnode 스타일)으로 바꾼다.

글 HTML 은 blog_html 의 스타일 상수로 만들어지므로, 예전 상수 문자열을 새 상수로 그대로 바꾸면 된다 (내용은 그대로).
대표 이미지는 같은 이름으로 새 디자인을 다시 올려 덮어쓴다 (글의 이미지 주소는 바뀌지 않는다).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import blog_html as new



def _value(old: str, replacement: str) -> tuple[str, str]:
    """style 속성값 전체 — 끝 따옴표까지 맞춰 바꾼다 (새 값이 예전 값으로 시작해도 두 번 바뀌지 않게)."""
    return f'{old}"', f'{replacement}"'


# 2026-09-29 이전 디자인 (참고 블로그 파랑·노랑·빨강 박스, 연보라 형광)
LEGACY: tuple[tuple[str, str], ...] = tuple(_value(o, r) for o, r in (
    ("background-color:#f0f7ff;border-left:4px solid #2196F3;padding:20px;margin:20px 0;border-radius:8px;"
     "line-height:1.8;", new.SUMMARY),
    ("background-color:#fff3cd;border-left:4px solid #ffc107;padding:15px 20px;margin:20px 0;border-radius:5px;"
     "line-height:1.8;", new.INFO),
    ("background-color:#f8d7da;border-left:4px solid #dc3545;padding:15px 20px;margin:20px 0;border-radius:5px;"
     "line-height:1.8;", new.WARNING),
    ("overflow-x:auto;-webkit-overflow-scrolling:touch;width:100%;margin:20px 0;", new.TABLE_WRAP),
    ("width:100%;border-collapse:collapse;font-size:15px;", new.TABLE),
    ("border:1px solid #ddd;padding:12px;text-align:left;background-color:#2196F3;color:#fff;font-weight:bold;",
     new.TH),
    ("border:1px solid #ddd;padding:12px;text-align:left;word-break:keep-all;", new.TD),
    ("background-color:#1e1e1e;border-radius:8px;padding:15px;margin:20px 0;overflow-x:auto;max-width:100%;"
     "text-align:left;", new.CODE_WRAP),
    ("margin:0;padding:12px;color:#d4d4d4;font-family:Consolas,Monaco,'Courier New',monospace;font-size:14px;"
     "line-height:1.6;white-space:pre;", new.CODE),
    ("background-color:#f5f5f5;padding:2px 6px;border-radius:4px;font-weight:600;color:#0066cc;", new.INLINE_CODE),
    ("background-color:#ede9fe;color:#1f1b3a;font-weight:700;padding:1px 5px;border-radius:4px;"
     "-webkit-box-decoration-break:clone;box-decoration-break:clone;", new.HIGHLIGHT),
    ("max-width:100%;height:auto;border-radius:8px;border:1px solid #eee;", new.FIGURE_IMG),
    ("margin:40px 0 20px;border:0;border-top:1px solid #ddd;",
     f"margin:40px 0 20px;border:0;border-top:1px solid {new.FOG};"),
)) + (
    ('style="margin:44px 0 16px;padding-bottom:8px;border-bottom:2px solid #e3f2fd;font-size:1.45em;"', new.H2),
    ('style="margin:28px 0 12px;font-size:1.2em;color:#0d47a1;"', new.H3),
    ('style="margin-top:8px;color:#666;font-size:0.9em;line-height:1.6;"', new.FIGCAPTION),
    ('style="color:#666;font-size:0.92em;line-height:1.7;margin:16px 0;"', new.NOTE),
    ('style="max-width:100%;height:auto;border-radius:8px;"/>',
     f'style="max-width:100%;height:auto;border-radius:14px;border:1px solid {new.FOG};"/>'),
    ('style="line-height:1.8;margin:0 0 18px;"', new.P),
)
_MARKER = re.compile(r"<!--yoopost (\{.*?\})-->", re.S)


@dataclass(frozen=True)
class Restyled:
    html: str
    changes: int

    @property
    def changed(self) -> bool:
        return self.changes > 0


def restyle_html(html: str) -> Restyled:
    """예전 스타일 문자열을 새 스타일로 바꾼다. 이미 새 디자인이면 그대로 (바뀐 곳 0)."""
    changes = 0
    for old, replacement in LEGACY:
        count = html.count(old)
        if count:
            html = html.replace(old, replacement)
            changes += count
    return Restyled(html, changes)


@dataclass(frozen=True)
class CoverInfo:
    url: str
    key: str  # 이미지 저장소 이름 (예: 20260929_1413_cover.png)
    title: str
    date_text: str  # 2026.09.29


def find_cover(html: str, host_url: str) -> CoverInfo | None:
    """글 맨 위 대표 이미지 — 이미지 저장소에 올린 <이름>_cover.png 만 (주소·제목·날짜)."""
    base = re.escape(host_url.rstrip("/"))
    match = re.search(rf'<img src="({base}/img/((\d{{4}})(\d{{2}})(\d{{2}})_\d{{4}}_cover\.png))" alt="([^"]*)"', html)
    if not match:
        return None
    url, key, year, month, day, title = match.groups()
    return CoverInfo(url, key, _unescape(title), f"{year}.{month}.{day}")


def versioned_key(key: str) -> str:
    """새 디자인 대표 이미지의 새 이름 — 이미지 저장소는 1년 캐시라 같은 이름에 덮어쓰면 바뀌지 않는다."""
    return key.replace("_cover.png", "_cover_v2.png")


def marker_category(html: str, labels: list[str], categories: tuple[str, ...]) -> str:
    """글의 카테고리 — 발행 기록 표시(주석) 우선, 없으면 카테고리 라벨."""
    match = _MARKER.search(html)
    if match:
        try:
            category = str(json.loads(match.group(1)).get("category") or "")
        except json.JSONDecodeError:
            category = ""
        if category in categories:
            return category
    return next((label for label in labels if label in categories), categories[0])


def _unescape(text: str) -> str:
    import html as html_lib

    return html_lib.unescape(text)
