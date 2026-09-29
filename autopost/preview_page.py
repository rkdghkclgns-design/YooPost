"""구글 블로그에 올라갈 모습 그대로의 미리보기 페이지 (파일 하나 — 대표 이미지는 data URI 로 포함).

슬랙 원고 채널에 올려 발행 전 검토용으로 쓴다.
"""
from __future__ import annotations

import base64
import html
import io
from dataclasses import replace
from pathlib import Path

from PIL import Image

from .blog_html import PostExtras, build_blog_html
from .content import PostContent

PREVIEW_MAX_PX = 900

PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ margin:0; background:#f4f5f7; font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif; color:#222; }}
  main {{ max-width:780px; margin:0 auto; background:#fff; padding:24px 20px 48px; font-size:16px; }}
  h1 {{ font-size:1.6em; line-height:1.4; margin:8px 0 24px; }}
  .note {{ background:#fff8e1; border:1px solid #f0d58a; border-radius:8px; padding:10px 14px; font-size:.9em; margin-bottom:20px; }}
</style></head>
<body><main>
<div class="note">{note}</div>
<h1>{title}</h1>
{body}
</main></body></html>
"""


def data_uri(path: Path, max_px: int = PREVIEW_MAX_PX) -> str:
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    image.thumbnail((max_px, max_px))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def render_preview(content: PostContent, extras: PostExtras, cover: Path | None, note: str) -> str:
    local = replace(extras, cover_url=data_uri(cover) if cover is not None and cover.is_file() else None)
    return PAGE.format(title=html.escape(content.title), note=html.escape(note), body=build_blog_html(content, local))


def write_page(out_path: Path, page: str) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return out_path
