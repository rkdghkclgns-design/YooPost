"""사실 검증 — 원고에서 검증 가능한 주장을 뽑아, 원고를 쓴 것과 별개로 웹 검색으로 다시 확인한다.

    1) 추출: 수치·날짜·가격·이름·스펙·출시 여부처럼 참/거짓을 가릴 수 있는 주장을 모두 뽑는다
    2) 검증: 구글 검색으로 주장마다 supported(확인) / contradicted(틀림) / unverifiable(확인 불가) 판정
판정이 모두 supported 여야 통과 — 파이프라인은 통과할 때까지 원고를 고치고, 끝내 통과하지 못하면 발행하지 않는다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Sequence

from .content import ContentError, PostContent, load_json_object, strip_code_fence
from .gemini import Gemini
from .research import FactSheet

log = logging.getLogger(__name__)

VERDICTS = ("supported", "contradicted", "unverifiable")
BATCH = 12  # 한 번에 검증할 주장 수 (많으면 판정이 대충 되기 쉬움)
CHUNK_CHARS = 3000  # 주장 추출은 원고를 이 크기로 나눠 빠짐없이 (긴 글 뒷부분이 검증에서 빠지지 않도록)
CLAIMS_PER_CHUNK = 30
MAX_CLAIMS = 150  # 이보다 많으면 넘치는 주장은 '확인 불가'로 처리 (검증 없이 통과시키지 않음)
OVER_LIMIT_NOTE = "검증 한도를 넘어 확인하지 못함 — 원고를 줄여 주세요"
VERDICT_LABELS = {"supported": "✅ 확인", "contradicted": "❌ 틀림", "unverifiable": "❔ 확인 불가"}


@dataclass(frozen=True)
class ClaimVerdict:
    claim: str
    verdict: str
    evidence: str = ""  # 근거 주소 또는 출처 번호
    correction: str = ""  # 틀렸을 때 올바른 내용

    @property
    def ok(self) -> bool:
        return self.verdict == "supported"

    def as_issue(self) -> dict:
        return {"claim": self.claim, "verdict": self.verdict, "evidence": self.evidence, "correction": self.correction}


@dataclass(frozen=True)
class FactCheckReport:
    verdicts: tuple[ClaimVerdict, ...]
    rounds: int = 1

    @property
    def issues(self) -> tuple[ClaimVerdict, ...]:
        return tuple(v for v in self.verdicts if not v.ok)

    @property
    def passed(self) -> bool:
        return bool(self.verdicts) and not self.issues

    def count(self, verdict: str) -> int:
        return sum(1 for v in self.verdicts if v.verdict == verdict)

    def summary_line(self) -> str:
        total = len(self.verdicts)
        line = f"주장 {total}개 중 {self.count('supported')}개 확인"
        extras = [f"틀림 {self.count('contradicted')}", f"확인 불가 {self.count('unverifiable')}"]
        extras = [e for e in extras if not e.endswith(" 0")]
        return line + (f" · {' · '.join(extras)}" if extras else "") + f" (검증 {self.rounds}회)"

    def markdown(self, title: str) -> str:
        lines = [f"# 사실 검증 보고 — {title}", "", self.summary_line(), ""]
        for v in self.verdicts:
            lines.append(f"- {VERDICT_LABELS.get(v.verdict, v.verdict)} {v.claim}")
            if v.evidence:
                lines.append(f"  - 근거: {v.evidence}")
            if v.correction:
                lines.append(f"  - 바로잡기: {v.correction}")
        return "\n".join(lines) + "\n"


def chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """줄 단위로 limit 글자 안팎씩 나눈다 (한 줄이 limit 보다 길면 그 줄 하나가 한 조각)."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if current and size + len(line) > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if any(line.strip() for line in current):
        chunks.append("\n".join(current))
    return chunks


def build_extract_prompt(text: str, title: str) -> str:
    return f"""아래는 블로그 원고 '{title}'의 일부입니다. 이 부분에서 참/거짓을 확인할 수 있는 사실 주장을 빠짐없이 뽑으세요.
- 대상: 수치(파라미터·가격·점수·길이·용량), 날짜, 회사·제품·모델·사람 이름과 관계, 기능·스펙, 출시·공개·제공 여부, 인용.
- 제외: 의견·전망·권유, 일반 상식, 예제 프롬프트·코드 내용.
- 비슷한 주장은 하나로 합치고, 각 주장은 원고 내용을 그대로 담은 한 문장으로 씁니다 (주어를 생략하지 말 것). 최대 {CLAIMS_PER_CHUNK}개.

[원고 일부]
{text}

[출력] JSON 객체 하나만: {{"claims": ["주장1", "주장2"]}}"""


def build_verify_prompt(claims: Sequence[str], sheet: FactSheet, today: str) -> str:
    listing = "\n".join(f"{i}. {c}" for i, c in enumerate(claims, start=1))
    sources = "\n".join(f"[{s.id}] {s.publisher} — {s.url}" for s in sheet.sources)
    return f"""당신은 까다로운 팩트체커입니다. 오늘은 {today} 입니다. 주제: {sheet.topic.title}
아래 주장을 하나씩 웹 검색으로 직접 확인하세요. 참고용 출처 목록이 있지만, 그 목록을 믿지 말고 스스로 검색해 확인합니다.

[판정 기준]
- supported: 공식 발표·공식 문서·논문·신뢰할 수 있는 언론이 그 내용을 명시적으로 말함 (수치·날짜가 정확히 일치)
- contradicted: 신뢰할 수 있는 자료가 다른 내용을 말함 → correction 에 올바른 내용
- unverifiable: 찾을 수 없거나 자료끼리 엇갈림
- 반올림·단위 차이 정도는 supported, 숫자 자체가 다르면 contradicted

[참고 출처]
{sources}

[주장]
{listing}

[출력] JSON 객체 하나만 (설명 문장 금지). results 는 위 주장 {len(claims)}개 모두, 번호 순서대로:
{{"results": [{{"n": 1, "claim": "주장 원문", "verdict": "supported|contradicted|unverifiable", "evidence": "근거 주소", "correction": ""}}]}}"""


def load_results(text: str) -> list:
    """판정 목록 — {"results": [...]}, {"verdicts": [...]}, 또는 목록만 온 경우 모두 받는다.

    검색 도구를 켠 응답은 JSON 강제 출력이 안 돼 형식이 조금씩 흔들린다 (실제 실행에서 확인).
    """
    body = strip_code_fence(text).strip()
    start = body.find("[")
    if start != -1 and (body.find("{") == -1 or start < body.find("{")):
        try:
            data = json.loads(body[start:body.rfind("]") + 1])
        except json.JSONDecodeError as exc:
            raise ContentError(f"판정 목록이 JSON 이 아닙니다: {exc}") from exc
        return data if isinstance(data, list) else []
    data = load_json_object(body)
    results = data.get("results") or data.get("verdicts") or data.get("claims")
    if not isinstance(results, list):
        raise ContentError("results 목록이 없습니다")
    return results


def _number(value: object) -> int | None:
    text = str(value if value is not None else "").strip().rstrip(".")
    return int(text) if text.isdigit() else None


def _match_claim(text: object, claims: Sequence[str]) -> int | None:
    """번호 없이 주장 문장만 돌려준 경우 — 가장 비슷한 주장의 번호 (충분히 비슷할 때만)."""
    wanted = str(text or "").strip()
    if not wanted:
        return None
    ratios = [SequenceMatcher(None, wanted, claim).ratio() for claim in claims]
    best = max(range(len(claims)), key=lambda i: ratios[i]) if claims else -1
    return best + 1 if best >= 0 and ratios[best] >= 0.8 else None


def index_results(results: Sequence, claims: Sequence[str]) -> dict[int, dict]:
    """판정을 주장 번호에 맞춘다: 번호 → 주장 문장 → (둘 다 없으면) 순서."""
    by_number: dict[int, dict] = {}
    for position, row in enumerate(results, start=1):
        if not isinstance(row, dict):
            continue
        number = _number(row.get("n", row.get("index", row.get("id"))))
        if number is None:
            number = _match_claim(row.get("claim"), claims) or position
        if 1 <= number <= len(claims) and number not in by_number:
            by_number[number] = row
    return by_number


class FactChecker:
    def __init__(self, gemini: Gemini) -> None:
        self._gemini = gemini

    def extract_claims(self, content: PostContent) -> list[str]:
        """원고 전체를 조각내 조각마다 주장을 뽑는다 (중복 제거, 순서 유지)."""
        def parse(text: str) -> list[str]:
            claims = load_json_object(text).get("claims")
            if not isinstance(claims, list):
                raise ContentError("claims 목록이 없습니다")
            return [str(c).strip() for c in claims if str(c).strip()]

        claims: list[str] = []
        for chunk in chunk_text(content.claim_text()):
            found, _ = self._gemini.generate(build_extract_prompt(chunk, content.title), parse,
                                             temperature=0.1, label="주장 추출")
            claims.extend(found)
        return list(dict.fromkeys(claims))

    def _verify_batch(self, claims: Sequence[str], sheet: FactSheet, today: str) -> list[ClaimVerdict]:
        def parse(text: str) -> dict[int, dict]:
            by_number = index_results(load_results(text), claims)
            if len(by_number) < len(claims) // 2:  # 절반도 판정하지 않았으면 다시
                raise ContentError(f"판정 누락: {len(by_number)}/{len(claims)}")
            return by_number

        by_number, _ = self._gemini.generate(
            build_verify_prompt(claims, sheet, today), parse, grounded=True, temperature=0.0, label="사실 검증"
        )
        verdicts = []
        for number, claim in enumerate(claims, start=1):
            row = by_number.get(number) or {}
            verdict = str(row.get("verdict") or "unverifiable").strip().lower()
            verdicts.append(ClaimVerdict(
                claim=claim,
                verdict=verdict if verdict in VERDICTS else "unverifiable",
                evidence=str(row.get("evidence") or "").strip(),
                correction=str(row.get("correction") or "").strip(),
            ))
        return verdicts

    def check(self, content: PostContent, sheet: FactSheet, today: str, rounds: int = 1) -> FactCheckReport:
        claims = self.extract_claims(content)
        if not claims:
            raise ContentError("원고에서 검증할 주장을 찾지 못했습니다")
        checked, overflow = claims[:MAX_CLAIMS], claims[MAX_CLAIMS:]
        verdicts: list[ClaimVerdict] = []
        for start in range(0, len(checked), BATCH):
            verdicts.extend(self._verify_batch(checked[start:start + BATCH], sheet, today))
        # 한도를 넘는 주장은 검증하지 않고 통과시키지 않는다 (fail closed) → 수정 단계에서 원고가 줄어든다
        verdicts.extend(ClaimVerdict(claim, "unverifiable", correction=OVER_LIMIT_NOTE) for claim in overflow)
        report = FactCheckReport(tuple(verdicts), rounds)
        log.info("사실 검증: %s", report.summary_line())
        return report
