"""한글 폰트 탐색 (Ubuntu: fonts-nanum / Windows: 맑은 고딕 / macOS: Apple SD 고딕)."""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

log = logging.getLogger(__name__)

BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "C:/Windows/Fonts/malgunbd.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
)
REGULAR_CANDIDATES = (
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/malgun.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
)


@lru_cache(maxsize=4)
def find_font_path(bold: bool = True) -> str | None:
    override = os.environ.get("FONT_PATH", "").strip()
    if override and Path(override).is_file():
        return override
    for candidate in BOLD_CANDIDATES if bold else REGULAR_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def load_font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    path = find_font_path(bold)
    if path is None:
        log.warning("한글 폰트를 찾지 못했습니다. 글자가 깨질 수 있습니다 (FONT_PATH 환경변수로 지정 가능)")
        return ImageFont.load_default(size=size)
    return ImageFont.truetype(path, size)
