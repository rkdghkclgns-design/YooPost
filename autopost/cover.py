"""대표 이미지 (1200×630, 참고 블로그 대표 이미지와 같은 비율) — 글 제목·카테고리·블로그 이름."""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw

from .fonts import load_font

WIDTH, HEIGHT = 1200, 630
TOP_COLOR = (13, 71, 161)  # 참고 블로그 강조색(#2196F3) 계열의 짙은 파랑
BOTTOM_COLOR = (33, 150, 243)
ACCENT = (255, 193, 7)  # 💡 박스 노랑(#ffc107)
MARGIN = 72
MAX_LINES = 3
# 폰트에 없는 이모지는 네모(□)로 찍히므로 이미지에는 넣지 않는다
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿️‍]")


def strip_emoji(text: str) -> str:
    return re.sub(r"\s+", " ", _EMOJI.sub("", text)).strip()


def _gradient() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), TOP_COLOR)
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        ratio = y / (HEIGHT - 1)
        color = tuple(int(a + (b - a) * ratio) for a, b in zip(TOP_COLOR, BOTTOM_COLOR))
        draw.line([(0, y), (WIDTH, y)], fill=color)
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
    image = _gradient()
    draw = ImageDraw.Draw(image)
    badge_font, title_font, footer_font = load_font(30), load_font(64), load_font(30, bold=False)

    badge = strip_emoji(category) or "AI"
    badge_w = int(draw.textlength(badge, font=badge_font)) + 44
    draw.rounded_rectangle([MARGIN, MARGIN, MARGIN + badge_w, MARGIN + 56], radius=28, fill=ACCENT)
    draw.text((MARGIN + 22, MARGIN + 9), badge, font=badge_font, fill=(33, 33, 33))

    lines = wrap_title(draw, strip_emoji(title), title_font, WIDTH - MARGIN * 2)
    line_h = 84
    top = (HEIGHT - line_h * len(lines)) // 2 + 10
    for index, line in enumerate(lines):
        draw.text((MARGIN, top + index * line_h), line, font=title_font, fill=(255, 255, 255))

    draw.line([(MARGIN, HEIGHT - 110), (WIDTH - MARGIN, HEIGHT - 110)], fill=(255, 255, 255), width=2)
    draw.text((MARGIN, HEIGHT - 92), blog_name, font=footer_font, fill=(255, 255, 255))
    date_w = draw.textlength(date_text, font=footer_font)
    draw.text((WIDTH - MARGIN - date_w, HEIGHT - 92), date_text, font=footer_font, fill=(227, 242, 253))

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)
    return path
