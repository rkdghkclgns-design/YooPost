"""구글·네이버 블로그에 올라갈 모습 그대로의 미리보기 페이지 (파일 하나 — 이미지는 data URI 로 포함).

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
from .content import PostContent, bold_segments
from .naver_publisher import HEADING_RULE, NaverExtras, build_blocks

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

NAVER_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>[네이버] {title}</title>
<style>
  body {{ margin:0; background:#f5f6f7; font-family:'Nanum Gothic','나눔고딕','Malgun Gothic',sans-serif; color:#333; }}
  .top {{ background:#03c75a; color:#fff; padding:12px 16px; font-weight:bold; }}
  main {{ max-width:720px; margin:0 auto; background:#fff; padding:24px 16px 48px; }}
  .note {{ background:#fff8e1; border:1px solid #f0d58a; border-radius:8px; padding:10px 14px; font-size:.9em; margin-bottom:20px; }}
  h1 {{ font-size:1.6em; line-height:1.4; margin:4px 0 8px; font-weight:normal; }}
  .meta {{ color:#999; font-size:.85em; border-bottom:1px solid #eee; padding-bottom:16px; margin-bottom:28px; }}
  .body {{ text-align:{align}; }}
  p {{ margin:0; font-size:16px; line-height:1.8; min-height:1.8em; word-break:break-all; }}
  p.rule {{ color:#2196F3; letter-spacing:-1px; }}
  p.code {{ font-family:Consolas,monospace; font-size:14px; white-space:pre-wrap; }}
  img {{ display:block; max-width:100%; height:auto; margin:24px auto 12px; border-radius:8px; }}
  .tags {{ margin-top:36px; border-top:1px solid #eee; padding-top:16px; }}
  .tags span {{ display:inline-block; color:#03c75a; margin:0 10px 8px 0; font-size:.95em; }}
</style></head>
<body><div class="top">네이버 블로그 미리보기</div><main>
<div class="note">{note}</div>
<h1>{title}</h1>
<div class="meta">{blog} · 에디터에 입력되는 순서·줄바꿈 그대로</div>
<div class="body">
{body}
</div>
<div class="tags">{tags}</div>
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


def _rich_line(line: str) -> str:
    return "".join(f"<b>{html.escape(t)}</b>" if bold else html.escape(t) for t, bold in bold_segments(line))


def render_naver_preview(content: PostContent, extras: NaverExtras, note: str, blog: str, align: str = "left") -> str:
    """스마트에디터에 들어가는 순서(글 한 줄 = 한 문단, 빈 줄 = 빈 문단) 그대로 보여준다."""
    parts: list[str] = []
    for block in build_blocks(content, extras):
        if block.kind == "image" and block.image is not None:
            if block.image.is_file():
                parts.append(f'<img src="{data_uri(block.image)}" alt="">')
            continue
        if block.kind == "heading":
            rule = f'<p class="rule">{HEADING_RULE}</p>'
            parts += [rule, f"<p><b>{html.escape(block.text.strip())}</b></p>", rule, "<p>&nbsp;</p>"]
            continue
        css = ' class="code"' if block.kind == "code" else ""
        text = f"**▶ {block.text.strip()}**" if block.kind == "subheading" else block.text
        for line in text.split("\n"):
            if not line.strip():
                parts.append("<p>&nbsp;</p>")
            elif block.kind == "code":
                parts.append(f"<p{css}>{html.escape(line.rstrip())}</p>")
            else:
                parts.append(f"<p>{_rich_line(line.strip())}</p>")
        parts.append("<p>&nbsp;</p>")
    tags = "".join(f"<span>{html.escape(tag)}</span>" for tag in content.hashtags)
    return NAVER_PAGE.format(title=html.escape(content.title), note=html.escape(note), blog=html.escape(blog),
                             body="\n".join(parts), tags=tags, align=align)


def write_page(out_path: Path, page: str) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return out_path
