"""참고 블로그 문법 프로필(autopost/data/style_profile.json) → 원고 작성 규칙.

프로필은 scripts/analyze_reference_blog.py 가 참고 블로그 전체 게시물을 분석해 만든다.
파일이 없어도 동작하도록 분석 결과와 같은 기본값을 둔다.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PROFILE_PATH = Path(__file__).resolve().parent / "data" / "style_profile.json"
MAX_TARGET_CHARS = 12000  # 한 번에 쓰기에 너무 긴 글은 사실 오류가 늘어 상한을 둔다
DEFAULT_HEADINGS = (
    "📰 등장 배경", "⚙️ 공식 스펙 정리", "📊 벤치마크 성능", "💰 가격·무료 사용 조건", "💻 설치·사용 가이드",
    "🧪 성능 테스트 예제 프롬프트", "💡 활용 시나리오 3가지", "💬 커뮤니티 반응", "⚠️ 사용 전 반드시 알아야 할 제약",
)


@dataclass(frozen=True)
class StyleGuide:
    total_posts: int = 0
    target_chars: int = 8000
    h2_count: int = 10
    h3_count: int = 12
    info_boxes: int = 6
    warning_boxes: int = 2
    tables: int = 4
    code_blocks: int = 3
    faq_questions: int = 6
    references: int = 6
    headings: tuple[str, ...] = DEFAULT_HEADINGS
    title_emojis: tuple[str, ...] = ("🤖", "🆓", "🔥", "⚡", "🧠", "🛠️", "📱")


def _top(pairs: object, limit: int) -> tuple[str, ...]:
    if not isinstance(pairs, list):
        return ()
    return tuple(str(p[0]) for p in pairs[:limit] if isinstance(p, list) and p and str(p[0]).strip())


FIXED_HEADINGS = ("자주 묻는 질문", "함께 보면 좋은 글", "맺음말", "참고 문헌", "한눈에 보기")


def _usable(pattern: str) -> bool:
    """주제어를 빼고 나니 이모지·숫자만 남은 패턴('💻', '📰 4 …')은 버린다."""
    words = re.findall(r"[가-힣A-Za-z]{2,}", pattern)
    return len(words) >= 1 and not re.match(r"^\W*\d", pattern) and not any(f in pattern for f in FIXED_HEADINGS)


def load_style(category: str = "", min_chars: int = 5000, path: Path = PROFILE_PATH) -> StyleGuide:
    """최근 게시물 기준 문법 (카테고리별 소제목 패턴이 있으면 그것을 우선)."""
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("문법 프로필을 읽지 못해 기본값을 씁니다: %s", path)
        return StyleGuide(target_chars=max(min_chars, StyleGuide.target_chars))
    recent = profile.get("recent") or {}
    by_category = (profile.get("recent_by_category") or {}).get(category) or {}
    patterns = _top(by_category.get("top_h2_patterns"), 20) + _top(recent.get("top_h2_patterns"), 40)
    headings = tuple(dict.fromkeys(p for p in patterns if _usable(p)))[:18] or DEFAULT_HEADINGS

    def num(key: str, default: float) -> int:
        value = recent.get(key)
        return int(round(value)) if isinstance(value, (int, float)) and value > 0 else int(default)

    median_chars = num("median_chars_no_space", StyleGuide.target_chars)
    return StyleGuide(
        total_posts=int(profile.get("total_posts") or 0),
        target_chars=min(max(median_chars, min_chars), MAX_TARGET_CHARS),
        h2_count=num("median_h2", StyleGuide.h2_count),
        h3_count=num("median_h3", StyleGuide.h3_count),
        info_boxes=num("median_info_boxes", StyleGuide.info_boxes),
        warning_boxes=num("median_warning_boxes", StyleGuide.warning_boxes),
        tables=num("median_tables", StyleGuide.tables),
        code_blocks=num("median_code_blocks", StyleGuide.code_blocks),
        faq_questions=num("median_faq_questions", StyleGuide.faq_questions),
        references=num("median_references", StyleGuide.references),
        headings=headings,
        title_emojis=_top(recent.get("top_title_emoji"), 10) or StyleGuide.title_emojis,
    )
