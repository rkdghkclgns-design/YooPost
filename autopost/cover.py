"""대표 이미지 (1200×630, 참고 블로그 대표 이미지와 같은 비율) — 글 제목·카테고리·블로그 이름.

디자인: Hashnode 스타일(refero.design) — 종이색 면 위 가는 테두리 패널, 먹색 제목, 파란 강조색 하나, 그림자·그라데이션 없음.
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw

from .fonts import load_font

WIDTH, HEIGHT = 1200, 630
PAPER = (244, 245, 247)  # #f4f5f7 바탕
CANVAS = (249, 250, 251)  # #f9fafb 패널
FOG = (209, 212, 217)  # #d1d4d9 가는 테두리
INK = (22, 25, 28)  # #16191c 제목
SLATE = (123, 129, 135)  # #7b8187 바닥글
BLUE = (29, 82, 222)  # #1d52de 유일한 강조색
TAG_BG = (227, 235, 253)  # #e3ebfd 파랑을 옅게 (카테고리 태그)
MARGIN = 72
INSET = 28  # 패널 바깥 여백
MAX_LINES = 3
# 폰트에 없는 이모지는 네모(□)로 찍히므로 이미지에는 넣지 않는다
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿️‍]")


def strip_emoji(text: str) -> str:
    return re.sub(r"\s+", " ", _EMOJI.sub("", text)).strip()


def _canvas() -> Image.Image:
    """종이색 바탕 위에 18px 둥근 패널 (가는 테두리) — 그라데이션·그림자 없음."""
    image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([INSET, INSET, WIDTH - INSET, HEIGHT - INSET], radius=18, fill=CANVAS, outline=FOG, width=2)
    return image


def wrap_title(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int = MAX_LINES) -> list[str]:
    """어절 단위로 줄바꿈 (한 어절이 너무 길면 글자 단위). 넘치면 마지막 줄을 … 로 줄인다."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = word
        while draw.textlength(current, font=font) > max_width and len(current) > 1:
            cut = len(current) - 1
            while cut > 1 and draw.textlength(current[:cut], font=font) > max_width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def make_cover(path: Path, title: str, category: str, blog_name: str, date_text: str) -> Path:
    image = _canvas()
    draw = ImageDraw.Draw(image)
    badge_font, title_font, footer_font = load_font(28), load_font(62), load_font(28, bold=False)

    badge = strip_emoji(category) or "AI"  # 카테고리 태그: 옅은 파랑 바탕 + 파란 점 + 파란 글씨 (10px 둥근 사각)
    badge_w = int(draw.textlength(badge, font=badge_font)) + 64
    draw.rounded_rectangle([MARGIN, MARGIN, MARGIN + badge_w, MARGIN + 52], radius=10, fill=TAG_BG)
    draw.ellipse([MARGIN + 20, MARGIN + 21, MARGIN + 30, MARGIN + 31], fill=BLUE)
    draw.text((MARGIN + 42, MARGIN + 9), badge, font=badge_font, fill=BLUE)

    lines = wrap_title(draw, strip_emoji(title), title_font, WIDTH - MARGIN * 2)
    line_h = 82
    top = (HEIGHT - line_h * len(lines)) // 2 + 6
    for index, line in enumerate(lines):
        draw.text((MARGIN, top + index * line_h), line, font=title_font, fill=INK)

    draw.line([(MARGIN, HEIGHT - 116), (WIDTH - MARGIN, HEIGHT - 116)], fill=FOG, width=2)
    draw.rectangle([MARGIN, HEIGHT - 117, MARGIN + 64, HEIGHT - 114], fill=BLUE)  # 파란 강조선 하나
    draw.text((MARGIN, HEIGHT - 98), blog_name, font=footer_font, fill=INK)
    date_w = draw.textlength(date_text, font=footer_font)
    draw.text((WIDTH - MARGIN - date_w, HEIGHT - 98), date_text, font=footer_font, fill=SLATE)

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)
    return path
