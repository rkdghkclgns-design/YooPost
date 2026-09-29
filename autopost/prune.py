"""사실 검증을 끝내 통과하지 못한 주장만 원고에서 빼기 — 글 전체를 보류하지 않고 발행이 이어지도록.

    1) 원고를 문장·목록 항목·표의 행·FAQ 단위로 나누고 번호(U1, U2 …)를 붙인다
    2) Gemini 에게 '틀림·확인 불가 주장이 들어 있는 번호'만 고르게 한다 (같은 내용이 여러 곳이면 모두)
       Gemini 가 실패하면 숫자·낱말이 겹치는 정도로 코드가 직접 찾는다
    3) 고른 번호의 문장·항목만 코드가 지운다 — AI 가 원고를 다시 쓰지 않으므로 새 주장이 끼어들 수 없다
소제목·코드블록·이미지·제목은 지우지 않는다. 문장이 모두 빠진 박스·섹션은 함께 정리한다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Sequence

from .constants import CLOSING_SIGNATURE
from .content import Block, ContentError, PostContent, Section, load_json_object, strip_inline
from .fact_check import ClaimVerdict
from .gemini import Gemini

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")
_TOKEN = re.compile(r"[0-9][0-9.,]*[%a-zA-Z가-힣]*|[A-Za-z][A-Za-z0-9\-+.]+|[가-힣]{2,}")
TEXT_KINDS = ("paragraph", "info", "warning")
HEURISTIC_OVERLAP = 0.6


@dataclass(frozen=True)
class Unit:
    id: str
    text: str
    path: tuple  # 원고 안 위치


@dataclass(frozen=True)
class PruneResult:
    content: PostContent
    removed: tuple[str, ...]  # 지운 문장·항목 (보고용)
    unlocated: tuple[str, ...]  # 원고에서 위치를 찾지 못한 주장


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text.strip()) if s.strip()]


def _parts(text: str) -> list[list[str]]:
    """문단(빈 줄) → 문장 목록."""
    return [sentences(part) for part in text.split("\n\n") if part.strip()]


def units_of(content: PostContent) -> list[Unit]:
    units: list[tuple[str, tuple]] = []
    for p, part in enumerate(_parts(content.intro)):
        units += [(s, ("intro", p, i)) for i, s in enumerate(part)]
    units += [(item, ("summary", i)) for i, item in enumerate(content.summary)]
    for s_no, section in enumerate(content.sections):
        for b_no, block in enumerate(section.blocks):
            if block.kind in TEXT_KINDS:
                for p, part in enumerate(_parts(block.text)):
                    units += [(s, ("block", s_no, b_no, p, i)) for i, s in enumerate(part)]
            elif block.kind == "list":
                units += [(item, ("item", s_no, b_no, i)) for i, item in enumerate(block.items)]
            elif block.kind == "table":
                units += [(" | ".join(row), ("row", s_no, b_no, i)) for i, row in enumerate(block.rows)]
    units += [(f"Q. {f.question} A. {f.answer}", ("faq", i)) for i, f in enumerate(content.faq)]
    for p, part in enumerate(_parts(content.closing)):
        units += [(s, ("closing", p, i)) for i, s in enumerate(part)]
    return [Unit(f"U{n}", strip_inline(text), path) for n, (text, path) in enumerate(units, start=1)]


def _keep_text(text: str, section: str, drop: set[tuple], *prefix) -> str:
    kept = []
    for p, part in enumerate(_parts(text)):
        remaining = [s for i, s in enumerate(part) if (section, *prefix, p, i) not in drop]
        if remaining:
            kept.append(" ".join(remaining))
    return "\n\n".join(kept)


def _prune_block(block: Block, s_no: int, b_no: int, drop: set[tuple]) -> Block | None:
    if block.kind in TEXT_KINDS:
        text = _keep_text(block.text, "block", drop, s_no, b_no)
        return replace(block, text=text) if text else None
    if block.kind == "list":
        items = tuple(item for i, item in enumerate(block.items) if ("item", s_no, b_no, i) not in drop)
        return replace(block, items=items) if items else None
    if block.kind == "table":
        rows = tuple(row for i, row in enumerate(block.rows) if ("row", s_no, b_no, i) not in drop)
        return replace(block, rows=rows) if rows else None
    return block


def remove_units(content: PostContent, paths: set[tuple]) -> PostContent:
    """고른 위치의 문장·항목만 지운 새 원고 (내용이 모두 빠진 박스·섹션은 함께 정리)."""
    sections: list[Section] = []
    for s_no, section in enumerate(content.sections):
        blocks = [b for b in (_prune_block(b, s_no, b_no, paths) for b_no, b in enumerate(section.blocks))
                  if b is not None]
        while blocks and blocks[-1].kind == "subheading":  # 뒤따르는 내용이 없어진 소제목
            blocks.pop()
        if any(b.kind not in ("subheading", "image") for b in blocks):
            sections.append(replace(section, blocks=tuple(blocks)))
    return replace(
        content,
        intro=_keep_text(content.intro, "intro", paths),
        summary=tuple(item for i, item in enumerate(content.summary) if ("summary", i) not in paths),
        sections=tuple(sections),
        faq=tuple(f for i, f in enumerate(content.faq) if ("faq", i) not in paths),
        closing=_keep_text(content.closing, "closing", paths) or CLOSING_SIGNATURE,
    )


def build_locate_prompt(units: Sequence[Unit], claims: Sequence[str]) -> str:
    unit_lines = "\n".join(f"{u.id}: {u.text}" for u in units)
    claim_lines = "\n".join(f"C{i}: {c}" for i, c in enumerate(claims, start=1))
    return f"""아래는 블로그 원고를 문장·항목 단위로 나눈 목록(U 번호)과, 사실 검증에서 틀렸거나 확인되지 않은 주장 목록(C 번호)입니다.
각 주장이 들어 있는 원고 항목 번호를 모두 찾으세요.
- 같은 내용이 여러 곳(도입·요약·본문·표·FAQ·맺음말)에 되풀이되면 그 번호를 모두 고릅니다.
- 주장과 관계없는 항목은 고르지 않습니다. 원고에 없는 주장이면 빈 배열입니다.

[원고 항목]
{unit_lines}

[문제 주장]
{claim_lines}

[출력] JSON 객체 하나만: {{"matches": [{{"claim": "C1", "units": ["U3", "U17"]}}]}}"""


def _tokens(text: str) -> set[str]:
    return {t.lower().rstrip(".,") for t in _TOKEN.findall(strip_inline(text))}


def heuristic_matches(units: Sequence[Unit], claim: str) -> list[str]:
    """숫자·낱말이 많이 겹치는 항목 (숫자가 있는 주장은 숫자가 하나 이상 같아야 함)."""
    wanted = _tokens(claim)
    numbers = {t for t in wanted if t[0].isdigit()}
    if not wanted:
        return []
    found = []
    for unit in units:
        have = _tokens(unit.text)
        overlap = len(wanted & have) / len(wanted)
        if overlap >= HEURISTIC_OVERLAP and (not numbers or numbers & have):
            found.append(unit.id)
    return found


class Pruner:
    def __init__(self, gemini: Gemini) -> None:
        self._gemini = gemini

    def _locate(self, units: Sequence[Unit], claims: Sequence[str]) -> dict[int, set[str]]:
        valid = {u.id for u in units}

        def parse(text: str) -> dict[int, set[str]]:
            matches = load_json_object(text).get("matches")
            if not isinstance(matches, list):
                raise ContentError("matches 목록이 없습니다")
            found: dict[int, set[str]] = {}
            for row in matches:
                if not isinstance(row, dict):
                    continue
                number = str(row.get("claim") or "").strip().upper().lstrip("C")
                ids = {str(u).strip().upper() for u in row.get("units") or []} & valid
                if number.isdigit() and 1 <= int(number) <= len(claims) and ids:
                    found.setdefault(int(number), set()).update(ids)
            return found

        try:
            found, _ = self._gemini.generate(build_locate_prompt(units, claims), parse, temperature=0.0,
                                             label="문제 문장 찾기")
        except Exception as exc:  # AI 가 못 하면 코드가 직접 찾는다
            log.warning("문제 문장 찾기 실패 — 낱말 비교로 찾습니다: %s", str(exc)[:200])
            found = {}
        for number, claim in enumerate(claims, start=1):
            if number not in found:
                ids = heuristic_matches(units, claim)
                if ids:
                    found[number] = set(ids)
        return found

    def prune(self, content: PostContent, issues: Sequence[ClaimVerdict]) -> PruneResult:
        claims = [v.claim for v in issues]
        units = units_of(content)
        found = self._locate(units, claims) if claims else {}
        by_id = {u.id: u for u in units}
        chosen = sorted({i for ids in found.values() for i in ids}, key=lambda i: int(i[1:]))
        unlocated = tuple(c for n, c in enumerate(claims, start=1) if n not in found)
        pruned = remove_units(content, {by_id[i].path for i in chosen})
        log.info("문제 항목 제외: 주장 %d개 → 문장·항목 %d곳 삭제 (위치 못 찾음 %d)", len(claims), len(chosen),
                 len(unlocated))
        return PruneResult(pruned, tuple(by_id[i].text for i in chosen), unlocated)


def removed_markdown(removed: Sequence[str], unlocated: Sequence[str] = ()) -> str:
    """사실 검증 보고(factcheck.md) 끝에 붙이는 '발행 전 제외한 문장' 목록."""
    lines = ["", f"## 발행 전 제외한 문장·항목 ({len(removed)}곳)"] + [f"- {t}" for t in removed]
    if unlocated:
        lines += ["", "## 원고에서 위치를 찾지 못한 주장"] + [f"- {c}" for c in unlocated]
    return "\n".join(lines) + "\n"

