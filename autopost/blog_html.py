"""구글 블로그 본문 HTML — 참고 블로그의 박스·표·코드블록 디자인(색·여백)을 인라인 스타일로 옮긴다.

Blogger 는 글마다 <style> 을 보장하지 않아 모든 스타일을 태그에 직접 넣는다.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from .constants import (
    CLOSING_HEADING,
    FAQ_HEADING,
    REFERENCES_HEADING,
    RELATED_HEADING,
    SUMMARY_TITLE,
)
from .content import Block, PostContent, inline_html

# 글 디자인 — Hashnode 스타일(refero.design): 먹색 글자 · 종이색 면 · 가는 회색 테두리 · 파란 강조색 하나 · 그림자 없음
INK, SLATE, FOG, PAPER, STONE, CANVAS, BLUE = "#16191c", "#7b8187", "#d1d4d9", "#f4f5f7", "#e6e8eb", "#f9fafb", "#1d52de"
P = f'style="line-height:1.8;margin:0 0 18px;color:{INK};"'
H2 = (f'style="margin:48px 0 16px;padding-bottom:10px;border-bottom:1px solid {FOG};font-size:1.4em;'
      f'font-weight:700;letter-spacing:-0.02em;line-height:1.35;color:{INK};"')
H3 = f'style="margin:28px 0 12px;font-size:1.15em;font-weight:600;letter-spacing:-0.01em;color:{INK};"'
SUMMARY = (f"background-color:{CANVAS};border:1px solid {FOG};border-radius:14px;padding:20px 24px;margin:24px 0;"
           f"line-height:1.8;color:{INK};")
INFO = (f"background-color:{PAPER};border:1px solid {FOG};border-left:3px solid {BLUE};border-radius:10px;"
        f"padding:16px 20px;margin:20px 0;line-height:1.8;color:{INK};")
WARNING = (f"background-color:{PAPER};border:1px solid {FOG};border-left:3px solid #d93036;border-radius:10px;"
           f"padding:16px 20px;margin:20px 0;line-height:1.8;color:{INK};")
TABLE_WRAP = (f"overflow-x:auto;-webkit-overflow-scrolling:touch;width:100%;margin:24px 0;border:1px solid {FOG};"
              "border-radius:14px;")
TABLE = f"width:100%;border-collapse:collapse;font-size:15px;color:{INK};"
TH = (f"border-bottom:1px solid {FOG};padding:12px 14px;text-align:left;background-color:{PAPER};color:{INK};"
      "font-weight:600;white-space:nowrap;")
TD = f"border-top:1px solid {STONE};padding:12px 14px;text-align:left;word-break:keep-all;"
CODE_WRAP = (f"background-color:{INK};border-radius:10px;padding:14px;margin:20px 0;overflow-x:auto;"
             "max-width:100%;text-align:left;")
CODE = ("margin:0;padding:10px;color:#e6e8eb;font-family:'JetBrains Mono',Consolas,Monaco,'Courier New',monospace;"
        "font-size:14px;line-height:1.6;white-space:pre;")
INLINE_CODE = (f"background-color:{PAPER};border:1px solid {STONE};padding:1px 6px;border-radius:6px;"
               f"font-weight:500;color:{BLUE};")
# 중요 내용 강조 — 굵은 글씨 + 형광 배경 (디자인의 파란 강조색을 옅게, 줄이 바뀌어도 배경이 끊기지 않게)
HIGHLIGHT = (f"background-color:#e3ebfd;color:{INK};font-weight:700;padding:1px 5px;border-radius:4px;"
             "-webkit-box-decoration-break:clone;box-decoration-break:clone;")
FIGURE = 'style="margin:28px 0;text-align:center;"'
FIGURE_IMG = f"max-width:100%;height:auto;border-radius:14px;border:1px solid {FOG};"
FIGCAPTION = f'style="margin-top:8px;color:{SLATE};font-size:0.9em;line-height:1.6;"'

NOTE = f'style="color:{SLATE};font-size:0.92em;line-height:1.7;margin:16px 0;"'


@dataclass(frozen=True)
class ImageRef:
    """본문에 넣을 인용 이미지 — src 는 공개 주소(발행) 또는 data URI(미리보기)."""
    id: str
    src: str
    caption: str
    credit: str
    page_url: str


@dataclass(frozen=True)
class PostExtras:
    """본문 외에 붙는 것 — 대표 이미지·인용 이미지·함께 보면 좋은 글·작성 기준일 문구·발행 표시."""
    cover_url: str | None = None
    related: tuple[tuple[str, str], ...] = ()  # (제목, 주소)
    basis_note: str = ""
    fact_note: str = ""
    images: tuple[ImageRef, ...] = ()
    marker: Mapping[str, object] | None = None  # 발행 기록용 숨은 표시 (주제·원문 주소·출처)


def _inline(text: str) -> str:
    return (inline_html(text)
            .replace("<b>", f'<b style="{HIGHLIGHT}">')
            .replace("<code>", f'<code style="{INLINE_CODE}">'))


def _paragraphs(text: str) -> str:
    return "".join(f"<p {P}>{_inline(part.strip())}</p>" for part in text.split("\n\n") if part.strip())


def _box(style: str, icon: str, title: str, text: str) -> str:
    head = title.strip()
    if head and not head.startswith(icon):
        head = f"{icon} {head}"
    head_html = f"<b>{_inline(head)}</b> — " if head else f"{icon} "
    return f'<div style="{style}">{head_html}{_inline(text)}</div>'


def _table(block: Block) -> str:
    head = "".join(f'<th style="{TH}">{_inline(h)}</th>' for h in block.headers)
    body = "".join(
        "<tr>" + "".join(f'<td style="{TD}">{_inline(c)}</td>' for c in row) + "</tr>" for row in block.rows
    )
    return (f'<div style="{TABLE_WRAP}"><table style="{TABLE}"><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def figure_html(image: ImageRef) -> str:
    credit = html.escape(image.credit or "원문")
    source = (f'<a href="{html.escape(image.page_url, quote=True)}" target="_blank" rel="noopener noreferrer">'
              f"{credit}</a>") if image.page_url else credit
    return (f"<figure {FIGURE}><img src=\"{html.escape(image.src, quote=True)}\" "
            f'alt="{html.escape(image.caption, quote=True)}" loading="lazy" style="{FIGURE_IMG}"/>'
            f"<figcaption {FIGCAPTION}>▲ {html.escape(image.caption)} (출처: {source})</figcaption></figure>")


def block_html(block: Block, images: Mapping[str, ImageRef] | None = None) -> str:
    if block.kind == "image":
        image = (images or {}).get(block.image_id)
        return figure_html(image) if image is not None else ""
    if block.kind == "subheading":
        return f"<h3 {H3}>{_inline(block.text)}</h3>"
    if block.kind == "info":
        return _box(INFO, "💡", block.title, block.text)
    if block.kind == "warning":
        return _box(WARNING, "⚠️", block.title, block.text)
    if block.kind == "table":
        return _table(block)
    if block.kind == "code":
        return f'<div style="{CODE_WRAP}"><pre style="{CODE}"><code>{html.escape(block.text)}</code></pre></div>'
    if block.kind == "list":
        items = "".join(f'<li style="margin:6px 0;">{_inline(i)}</li>' for i in block.items)
        return f'<ul {P}>{items}</ul>'
    return _paragraphs(block.text)


def _summary(items: Sequence[str]) -> str:
    lines = "<br/>".join(f"- {_inline(item)}" for item in items)
    return f'<div style="{SUMMARY}"><b>{SUMMARY_TITLE}</b><br/>{lines}</div>'


def _links(items: Sequence[tuple[str, str]]) -> str:
    rows = "".join(
        f'<li style="margin:6px 0;"><a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">'
        f"{html.escape(title)}</a></li>" for title, url in items
    )
    return f'<ul style="line-height:1.8;">{rows}</ul>'


def marker_comment(marker: Mapping[str, object]) -> str:
    """발행 기록용 HTML 주석 — 구글 블로그 글 목록이 곧 발행 기록이다 (주석은 화면에 보이지 않는다)."""
    payload = json.dumps(dict(marker), ensure_ascii=True, separators=(",", ":")).replace("--", "\\u002d\\u002d")
    return f"<!--yoopost {payload}-->"


def build_blog_html(content: PostContent, extras: PostExtras = PostExtras()) -> str:
    images = {image.id: image for image in extras.images}
    placed: set[str] = set()
    parts = [_paragraphs(content.intro)]
    if extras.cover_url:
        parts.append(
            f'<div style="text-align:center;margin:24px 0;"><img src="{html.escape(extras.cover_url, quote=True)}" '
            f'alt="{html.escape(content.title, quote=True)}" style="max-width:100%;height:auto;border-radius:14px;border:1px solid {FOG};"/></div>'
        )
    if content.summary:
        parts.append(_summary(content.summary))
    for section in content.sections:
        parts.append(f"<h2 {H2}>{_inline(section.heading)}</h2>")
        for block in section.blocks:
            if block.kind == "image":  # 같은 이미지는 한 번만
                if block.image_id in placed:
                    continue
                placed.add(block.image_id)
            parts.append(block_html(block, images))
    if content.faq:
        parts.append(f"<h2 {H2}>{FAQ_HEADING}</h2>")
        parts.extend(
            f"<p {P}><b>Q{i}. {_inline(item.question)}</b><br/>{_inline(item.answer)}</p>"
            for i, item in enumerate(content.faq, start=1)
        )
    if extras.related:
        parts += [f"<h2 {H2}>{RELATED_HEADING}</h2>", _links(extras.related)]
    parts += [f"<h2 {H2}>{CLOSING_HEADING}</h2>", _paragraphs(content.closing)]
    if content.references:
        refs = "".join(
            f'<li style="margin:6px 0;">{html.escape(r.publisher)}. ({html.escape(r.date)}). '
            f"<i>{html.escape(r.title)}</i>. Retrieved from "
            f'<a href="{html.escape(r.url, quote=True)}" target="_blank" rel="noopener noreferrer" '
            f'style="word-break:break-all;">'
            f"{html.escape(r.url)}</a></li>"
            for r in content.references
        )
        parts += [f'<hr style="margin:40px 0 20px;border:0;border-top:1px solid {FOG};"/>',
                  f"<h2 {H2}>{REFERENCES_HEADING}</h2>", f'<ul style="line-height:1.7;">{refs}</ul>']
    notes = " ".join(n for n in (extras.basis_note, content.caveat, extras.fact_note) if n)
    if notes:
        parts.append(f"<p {NOTE}>{html.escape(notes)}</p>")
    if extras.marker:
        parts.append(marker_comment(extras.marker))
    return "\n".join(p for p in parts if p)
