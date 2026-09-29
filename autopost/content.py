"""AI 원고 데이터 구조 — 참고 블로그의 글 문법을 그대로 담는다.

    제목(이모지 + 핵심어 + 숫자) → 인사·도입 → 대표 이미지 → [한눈에 보기] 박스
    → 이모지 소제목(h2) 섹션들 (문단·h3·💡정보·⚠️주의 박스·표·코드블록·목록)
    → ❓ 자주 묻는 질문 → 🔗 함께 보면 좋은 글 → 🎯 맺음말 → 📚 참고 문헌 및 출처 → 작성 기준일

본문 안에서는 **굵게**, `코드` 두 가지 인라인 표시만 쓴다.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, replace
from typing import Sequence

from .constants import CATEGORIES

BLOCK_KINDS = ("paragraph", "subheading", "info", "warning", "table", "code", "list")
MAX_TITLE = 100
MAX_HASHTAGS = 15
MAX_HASHTAG_LEN = 30
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`\n]+)`")


class ContentError(RuntimeError):
    """AI 원고 형식 오류."""


@dataclass(frozen=True)
class Block:
    kind: str
    text: str = ""
    title: str = ""
    headers: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    language: str = ""
    items: tuple[str, ...] = ()

    def texts(self) -> list[str]:
        """사실 검증·분량 계산용 텍스트 (코드블록 제외)."""
        if self.kind == "code":
            return []
        return [t for t in (self.title, self.text, *self.headers, *self.items,
                            *(cell for row in self.rows for cell in row)) if t]


@dataclass(frozen=True)
class Section:
    heading: str
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class Faq:
    question: str
    answer: str


@dataclass(frozen=True)
class Reference:
    publisher: str
    date: str  # APA 표기 날짜 예) "2026, September 26" 또는 "2026"
    title: str
    url: str

    def apa(self) -> str:
        return f"{self.publisher}. ({self.date}). {self.title}. Retrieved from {self.url}"


@dataclass(frozen=True)
class PostContent:
    title: str
    category: str
    intro: str
    summary: tuple[str, ...]
    sections: tuple[Section, ...]
    faq: tuple[Faq, ...]
    closing: str
    hashtags: tuple[str, ...]
    references: tuple[Reference, ...] = ()
    source_ids: tuple[str, ...] = ()  # 원고가 인용한 출처 ID (S1, S2 …)
    caveat: str = ""  # 작성 기준일 문구 뒤에 붙는 주의 문구

    def body_texts(self) -> list[str]:
        texts = [self.intro, *self.summary, self.closing]
        for section in self.sections:
            texts.append(section.heading)
            for block in section.blocks:
                texts.extend(block.texts())
        for item in self.faq:
            texts.extend([item.question, item.answer])
        return [t for t in texts if t]

    def text_length(self) -> int:
        """본문 글자 수 (공백·코드블록·참고문헌 제외) — 분량 점검용."""
        return sum(len(re.sub(r"\s", "", strip_inline(t))) for t in self.body_texts())

    def claim_text(self) -> str:
        """사실 검증용 본문 (제목·요약·섹션·FAQ·맺음말)."""
        return "\n".join(strip_inline(t) for t in (self.title, *self.body_texts()))

    def with_references(self, references: Sequence[Reference]) -> "PostContent":
        return replace(self, references=tuple(references))

    def plain_text(self) -> str:
        """슬랙 원고 파일(post.txt)용 일반 텍스트."""
        lines = [self.title, "", strip_inline(self.intro), "", "[한눈에 보기]"]
        lines += [f"- {strip_inline(s)}" for s in self.summary] + [""]
        for section in self.sections:
            lines += [f"■ {section.heading}", ""]
            for block in section.blocks:
                lines += plain_block(block) + [""]
        if self.faq:
            lines += ["■ 자주 묻는 질문", ""]
            for index, item in enumerate(self.faq, start=1):
                lines += [f"Q{index}. {strip_inline(item.question)}", strip_inline(item.answer), ""]
        lines += ["■ 맺음말", strip_inline(self.closing), ""]
        if self.references:
            lines += ["■ 참고 문헌 및 출처"] + [f"- {r.apa()}" for r in self.references] + [""]
        lines.append(" ".join(self.hashtags))
        return "\n".join(lines).strip()


def plain_block(block: Block) -> list[str]:
    if block.kind == "subheading":
        return [f"▶ {strip_inline(block.text)}"]
    if block.kind in ("info", "warning"):
        icon = "💡" if block.kind == "info" else "⚠️"
        head = f"{icon} {strip_inline(block.title)} — " if block.title else f"{icon} "
        return [head + strip_inline(block.text)]
    if block.kind == "table":
        return [" | ".join(block.headers)] + [" | ".join(strip_inline(c) for c in row) for row in block.rows]
    if block.kind == "code":
        return ["```", *block.text.split("\n"), "```"]
    if block.kind == "list":
        return [f"• {strip_inline(item)}" for item in block.items]
    return [strip_inline(block.text)]


# ------------------------------------------------------------------ 인라인 표시
def strip_inline(text: str) -> str:
    """**굵게**, `코드` 표시를 지운 순수 텍스트."""
    return _CODE.sub(r"\1", _BOLD.sub(r"\1", text))


def inline_html(text: str) -> str:
    """HTML 이스케이프 후 **굵게** → <b>, `코드` → <code>, 줄바꿈 → <br/>."""
    escaped = html.escape(text)
    escaped = _BOLD.sub(r"<b>\1</b>", escaped)
    escaped = _CODE.sub(r"<code>\1</code>", escaped)
    return escaped.replace("\n", "<br/>")


# ------------------------------------------------------------------ AI 응답 해석
def strip_code_fence(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    return match.group(1) if match else text


def load_json_object(raw: str) -> dict:
    """AI 응답에서 JSON 객체 하나를 꺼낸다 (코드블록·앞뒤 설명이 붙어도)."""
    text = strip_code_fence(raw).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        text = text[start:end + 1] if start != -1 and end > start else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContentError(f"AI 응답이 JSON 형식이 아닙니다: {exc}") from exc
    if not isinstance(data, dict):
        raise ContentError("AI 응답 최상위가 객체가 아닙니다")
    return data


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContentError(f"AI 응답의 '{field}' 필드가 비어 있습니다")
    return value.strip()


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v).strip() for v in value if str(v).strip())


def _block(raw: object) -> Block | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or raw.get("kind") or "paragraph").strip()
    if kind not in BLOCK_KINDS:
        kind = "paragraph"
    if kind == "table":
        headers = _strings(raw.get("headers"))
        rows = tuple(
            tuple(str(c).strip() for c in row) for row in raw.get("rows") or [] if isinstance(row, list) and row
        )
        if not headers or not rows:
            return None
        width = len(headers)
        return Block("table", headers=headers, rows=tuple((row + ("",) * width)[:width] for row in rows))
    if kind == "list":
        items = _strings(raw.get("items"))
        return Block("list", items=items) if items else None
    if kind == "code":
        code = str(raw.get("code") or raw.get("text") or "").strip("\n")
        return Block("code", text=code, language=str(raw.get("language") or "")) if code.strip() else None
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    return Block(kind, text=text, title=str(raw.get("title") or "").strip())


def _section(raw: object) -> Section | None:
    if not isinstance(raw, dict):
        return None
    heading = str(raw.get("heading") or "").strip()
    blocks = tuple(b for b in (_block(item) for item in raw.get("blocks") or []) if b is not None)
    if not heading or not blocks:
        return None
    return Section(heading, blocks)


def normalize_hashtags(tags: Sequence[str], fallback: Sequence[str] = (), count: int = MAX_HASHTAGS) -> tuple[str, ...]:
    """'#' 통일, 공백/특수문자 제거, 중복 제거 후 최대 count개."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in (*tags, *fallback):
        cleaned = re.sub(r"[^0-9A-Za-z가-힣_.]", "", str(raw)).strip(".")[:MAX_HASHTAG_LEN]
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        result.append(f"#{cleaned}")
        if len(result) == count:
            break
    return tuple(result)


def parse_post(raw: str, default_category: str = CATEGORIES[0]) -> PostContent:
    data = load_json_object(raw)
    sections = tuple(s for s in (_section(item) for item in data.get("sections") or []) if s is not None)
    if len(sections) < 2:
        raise ContentError("AI 응답에 본문 섹션이 2개 미만입니다")
    faq = tuple(
        Faq(str(item.get("q") or item.get("question") or "").strip(),
            str(item.get("a") or item.get("answer") or "").strip())
        for item in data.get("faq") or [] if isinstance(item, dict)
    )
    category = str(data.get("category") or "").strip()
    return PostContent(
        title=_text(data.get("title"), "title")[:MAX_TITLE],
        category=category if category in CATEGORIES else default_category,
        intro=_text(data.get("intro"), "intro"),
        summary=_strings(data.get("summary")),
        sections=sections,
        faq=tuple(item for item in faq if item.question and item.answer),
        closing=_text(data.get("closing"), "closing"),
        hashtags=normalize_hashtags(_strings(data.get("hashtags")), ("AI", "인공지능", "AI뉴스")),
        source_ids=tuple(s.upper() for s in _strings(data.get("source_ids"))),
        caveat=str(data.get("caveat") or "").strip(),
    )
