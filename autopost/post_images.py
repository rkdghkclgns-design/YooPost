"""이미 발행한 글 본문에 인용 이미지(figure)를 끼워 넣는다 — 이미지 없이 나간 글을 고칠 때 (scripts/add_images.py).

본문 구조(build_blog_html): 도입 → 대표 이미지 → 한눈에 보기 → 소제목(h2)마다 블록 … → FAQ·관련 글·맺음말·참고 문헌(h2).
앞쪽 h2 들이 원고 소제목 순서와 같으므로, n번째 소제목 h2 뒤 첫 문단(</p>) 바로 다음에 그림을 넣는다.
"""
from __future__ import annotations

import re
from typing import Sequence

_H2 = re.compile(r"<h2\b", re.I)


def target_sections(section_count: int, image_count: int) -> list[int]:
    """그림을 넣을 소제목 번호 — 첫 소제목과 가운데 소제목 (writer.place_images 와 같은 규칙)."""
    if section_count <= 0 or image_count <= 0:
        return []
    if image_count == 1 or section_count == 1:
        return [0]
    return [0, max(1, section_count // 2)]


def insert_figures(html: str, figures: Sequence[str], section_count: int) -> str:
    """소제목 h2 뒤 첫 문단 다음에 figure 를 넣은 새 본문. 이미 figure 가 있으면 그대로 돌려준다."""
    if "<figure" in html or not figures:
        return html
    starts = [m.start() for m in _H2.finditer(html)][:section_count]
    places = target_sections(len(starts), len(figures))
    inserts: list[tuple[int, str]] = []
    for figure, index in zip(figures, places):
        h2 = starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else len(html)
        heading_end = html.find("</h2>", h2)
        if heading_end < 0:
            continue
        paragraph_end = html.find("</p>", heading_end, end)
        at = paragraph_end + len("</p>") if paragraph_end >= 0 else heading_end + len("</h2>")
        inserts.append((at, figure))
    for at, figure in sorted(inserts, reverse=True):  # 뒤에서부터 넣어 앞 위치가 밀리지 않게
        html = html[:at] + figure + html[at:]
    return html
