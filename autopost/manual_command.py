"""슬랙 #yoopost-0-수동지시 채널의 지시문 해석.

    "슬랙에 에이전트 직원 만들기를 AI도구에 발행해줘" → 한 편 (카테고리 AI 도구, 주제 '슬랙에 에이전트 직원 만들기')
    "AI 인사이트 한 편 더 발행해줘"                   → 한 편 (카테고리만, 주제는 작성자가 고름)
    "구글 Gemini 4 출시 소식 발행해줘"                → 한 편 (주제만, 카테고리는 작성자가 고름)
    "전체 새로운 포스트로 재발행"                     → 7개 카테고리 새 글 1편씩
    "도움말"                                          → 사용법

해석 결과는 클라우드 루틴에 JSON 으로 넘긴다 (fire_text). 원고는 여느 원고처럼 #yoopost-1-원고승인 에 올라오고 ✅ 로 발행된다.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass

from .constants import CATEGORIES

MAX_TOPIC = 200
KINDS = ("single", "all")

USAGE = "\n".join([
    "*🧑‍💼 수동 지시 사용법* — 이 채널에 한 줄로 적으면 Claude 가 원고를 씁니다 (클라우드, PC 꺼져도 됨).",
    "• `슬랙에 에이전트 직원 만들기를 AI도구에 발행해줘` — 주제·카테고리를 정해 1편",
    "• `AI 보안 한 편 더 발행해줘` — 카테고리만 정하면 주제는 Claude 가 새로 고름",
    "• `구글 Gemini 4 출시 소식 발행해줘` — 주제만 정하면 카테고리는 Claude 가 고름",
    "• `전체 새로운 포스트로 재발행` — 7개 카테고리마다 새 글 1편씩",
    "원고는 사실 검증을 거쳐 #yoopost-1-원고승인 에 올라오고, ✅ 를 눌러야 블로그에 발행됩니다. 진행 상황은 이 스레드에 달립니다.",
    "카테고리: " + " · ".join(CATEGORIES),
])

_HELP = re.compile(r"^(도움말|help|사용법|\?)$", re.IGNORECASE)
_MARKUP_USER = re.compile(r"<[@!](?:[^>|]*)(?:\|[^>]*)?>")
_MARKUP_LABEL = re.compile(r"<([^>|]+)\|([^>]*)>")
_MARKUP_BARE = re.compile(r"<([^>|]+)>")
_MARKDOWN = re.compile(r"[*_~`]")
_SPACES = re.compile(r"\s+")

_ALIASES: dict[str, tuple[str, ...]] = {
    "AI 언어 모델": (r"언어\s*모델", r"LLM"),
    "AI 도구": (r"도구",),
    "AI 논문 분석": (r"논문\s*분석", r"논문"),
    "AI 뉴스 리뷰": (r"뉴스\s*리뷰", r"뉴스"),
    "AI 보안": (r"보안",),
    "AI 인사이트": (r"인사이트",),
    "AI 공모주 분석": (r"공모주\s*분석", r"공모주", r"IPO"),
}
# 지시는 끝에 '발행해줘·써줘·올려줘' 같은 말이 있어야 한다 (잡담으로 유료 루틴이 돌지 않게)
_VERB = re.compile(
    r"\s*(?:재발행|발행|작성|포스팅|게시|업로드|올려|써)\s*(?:해\s*)?"
    r"(?:줘요|줘|주세요|줄래요|줄래|부탁해요|부탁해|부탁합니다)?\s*[.!~]*\s*$")
_MARK = r"(?:카테고리|분류|메뉴)"
_PARTICLE = r"(?:에다가|에다|에|으로|로)"
# '전체' 지시를 알리는 말과, 전체·카테고리만 정한 지시에서 주제가 아닌 말 (낱말 단위로 비교 — 정규식 되짚기 없음)
_ALL_MARKERS = ("전체", "전부", "모든", "모두", "카테고리별", "카테고리별로", "카테고리마다", "각", "각각")
_FILLER_WORDS = frozenset({
    "새", "새로", "새로운", "새글", "글", "글로", "포스트", "포스트로", "포스팅", "한", "편", "편씩", "한편", "한편씩",
    "하나", "하나씩", "씩", "더", "다시", "로", "으로", "카테고리", "카테고리에", "카테고리로", "개", "최신", "최근",
    *_ALL_MARKERS,
})
_COUNT = re.compile(r"^[0-9０-９]+(?:개|편|편씩|개씩)?$|^일곱(?:개|편)?$")
_OBJECT = re.compile(r"(?<=\S)(?:을|를)$")
_ID = re.compile(r"^(C[A-Z0-9]{4,20})-(\d{10})(\d{6})$")


@dataclass(frozen=True)
class ManualCommand:
    kind: str  # single | all | help | invalid
    category: str = ""  # 비어 있으면 작성자가 고름 (single)
    topic: str = ""  # 비어 있으면 작성자가 새 주제를 고름 (single)
    error: str = ""


def clean_text(text: str) -> str:
    """슬랙 표기(<@U..>, <url|글자>, *굵게*)를 걷어 낸 한 줄."""
    text = _MARKUP_USER.sub(" ", text or "")
    text = _MARKUP_LABEL.sub(lambda m: m.group(2) or m.group(1), text)
    text = _MARKUP_BARE.sub(lambda m: m.group(1), text)
    text = html.unescape(_MARKDOWN.sub("", text))  # 슬랙은 &, <, > 를 &amp; &lt; &gt; 로 보낸다
    return _SPACES.sub(" ", text).strip()


def _category_patterns() -> list[tuple[str, str, bool]]:
    """(카테고리, 정규식, 정식 이름인가) — 긴 이름부터."""
    patterns = []
    for name in CATEGORIES:
        full = r"AI\s*" + r"\s*".join(map(re.escape, name.split(" ")[1:]))
        patterns.append((name, full, True))
        patterns.extend((name, alias, False) for alias in _ALIASES.get(name, ()))
    return sorted(patterns, key=lambda p: -len(p[1]))


_PATTERNS = _category_patterns()


def _before(full: bool) -> str:
    """카테고리 말 앞 경계 — 'AI…' 는 한글 뒤에 붙어도 되고, 별칭('도구'·'보안')은 낱말 첫머리여야 한다."""
    return r"(?<![0-9A-Za-z])" if full else r"(?<![0-9A-Za-z가-힣])"


def _is_filler(text: str) -> bool:
    return all(w in _FILLER_WORDS or _COUNT.match(w) for w in text.split())


def _find_category(body: str) -> tuple[str, str]:
    """(카테고리, 주제로 쓸 나머지). 카테고리로 인정하는 자리는 셋뿐 — 주제 속 낱말('보안에 강한…')을 잘라 내지 않는다.

    1. 끝: '… AI도구에' · '… 뉴스 카테고리로' (동사 바로 앞, '에·로·카테고리' 필요)
    2. 앞머리: 'AI 보안 카테고리로 …' · '논문: …' ('카테고리·분류·메뉴' 또는 ':' 필요)
    3. 정식 이름만 있고 나머지가 '한 편 더' 같은 말뿐: 'AI 인사이트 한 편 더' → 카테고리만 정한 지시
    """
    for name, pattern, full in _PATTERNS:
        end = re.search(rf"{_before(full)}{pattern}(?:\s*{_MARK}\s*{_PARTICLE}?|\s*{_PARTICLE})\s*$", body, re.I)
        if end:
            return name, body[:end.start()]
    for name, pattern, full in _PATTERNS:
        head = re.match(rf"{pattern}\s*(?:{_MARK}\s*(?:{_PARTICLE}|:)?|:)(?=\s|$)", body, re.I)
        if head:
            return name, body[head.end():]
    for name, pattern, full in _PATTERNS:
        head = re.match(rf"{pattern}{_PARTICLE}?(?=\s|$)", body, re.I) if full else None
        if head and _is_filler(body[head.end():]):
            return name, ""
    return "", body


def _topic(rest: str) -> str:
    rest = _SPACES.sub(" ", rest).strip(" :,-·")
    if _is_filler(rest):
        return ""
    return _OBJECT.sub("", rest).strip()[:MAX_TOPIC].strip()


def _is_all(body: str) -> bool:
    words = body.split()
    return any(w in _ALL_MARKERS or re.match(r"^(?:[7７]|일곱)(?:개|편)", w) for w in words) and _is_filler(body)


NO_VERB = "지시로 보지 않았습니다 — 끝에 '발행해줘'·'써줘'·'올려줘' 를 붙여 주세요 (예: 슬랙에 에이전트 직원 만들기를 AI도구에 발행해줘)."


def parse_command(text: str) -> ManualCommand:
    cleaned = clean_text(text)
    if not cleaned or _HELP.match(cleaned):
        return ManualCommand("help")
    verb = _VERB.search(cleaned)
    if not verb:
        return ManualCommand("invalid", error=NO_VERB)
    body = cleaned[:verb.start()].strip(" :,-·")
    if body and _is_all(body):
        return ManualCommand("all")
    category, rest = _find_category(body)
    topic = _topic(rest)
    if not category and not topic:
        return ManualCommand("invalid", error="무엇을 쓸지 알 수 없습니다 — 주제나 카테고리를 적어 주세요.")
    return ManualCommand("single", category, topic)


def request_id(channel: str, ts: str) -> str:
    """슬랙 채널·메시지 시각 → 요청 ID (이 ID 로 그 메시지 스레드에 진행 상황을 단다)."""
    return f"{channel}-{ts.replace('.', '')}"


def split_request_id(value: str) -> tuple[str, str] | None:
    """요청 ID → (채널, 스레드 ts). 형식이 아니면 None."""
    match = _ID.match(value or "")
    return (match.group(1), f"{match.group(2)}.{match.group(3)}") if match else None


def fire_text(command: ManualCommand, rid: str) -> str:
    """클라우드 루틴 /fire 의 text — 요청 ID·종류·카테고리만 (주제는 원고 브랜치의 요청 기록에서 루틴이 읽는다).

    토큰이 새어도 원고 브랜치에 Actions 가 남긴 요청 기록과 맞지 않으면 루틴이 아무것도 하지 않는다.
    """
    if command.kind not in KINDS:
        raise ValueError(f"루틴에 넘길 수 없는 지시: {command.kind}")
    return json.dumps({"yoopost_manual_request": 1, "id": rid, "kind": command.kind, "category": command.category},
                      ensure_ascii=False)
