"""자료 조사 — 구글 딥리서치로 사실 목록(fact sheet)·인용 이미지 후보를 만들고, 출처 주소를 하나하나 확인한다.

1) 구글 딥리서치 에이전트가 수십 개 페이지를 직접 읽고 보고서 + JSON(사실·출처·이미지)을 쓴다.
   JSON 이 깨졌으면 Gemini 가 보고서를 JSON 으로 옮겨 적는다 (새 내용 추가 금지).
2) 딥리서치가 실패·시간 초과면 예전 방식(Gemini + 구글 검색 근거)으로 대신 조사한다 — 포스팅이 멈추지 않도록.

사실(fact)마다 출처 번호를 붙이고, 출처는 아래를 모두 통과해야 쓴다.
  - 실제로 열리는 주소 (404·도메인 없음 제외 → 지어낸 주소 차단)
  - 페이지에 주제어가 나옴 (엉뚱한 페이지 제외)
  - 봇 차단(403 등)으로 열어볼 수 없으면, 딥리서치·검색이 실제로 인용한 주소일 때만 인정
출처가 모두 탈락한 사실은 버리고, 남은 사실·출처가 기준보다 적으면 조사 실패로 멈춘다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

from .content import ContentError, load_json_object
from .deep_research import DeepResearch
from .gemini import Gemini
from .links import LinkCheck, canonical, check_link, domain, is_http_url, resolve_grounding_url, unique_urls
from .topic import Topic

log = logging.getLogger(__name__)

MIN_FACTS = 8
MAX_SOURCES = 12
MAX_CITED = 80  # 딥리서치 인용 주소 중 확인할 최대 개수
ENGINE_DEEP = "구글 딥리서치"
ENGINE_SEARCH = "구글 검색 (딥리서치 대체)"
_JSON_BLOCKS = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class ResearchError(RuntimeError):
    """사실 확인에 쓸 자료가 부족함."""


@dataclass(frozen=True)
class Source:
    id: str  # S1, S2 …
    title: str
    url: str
    publisher: str
    published: str = ""  # YYYY-MM-DD (모르면 빈 값)
    official: bool = False
    grounded: bool = False  # 구글 검색 근거로 확인된 주소


@dataclass(frozen=True)
class Fact:
    statement: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class ImageCandidate:
    """딥리서치가 찾은 인용 이미지 후보 (아직 확인 전)."""
    id: str  # I1, I2 …
    image_url: str
    page_url: str
    caption: str
    credit: str = ""


@dataclass(frozen=True)
class QuoteImage:
    """내려받아 확인한 인용 이미지 — 본문에 출처와 함께 넣는다."""
    id: str
    caption: str
    credit: str
    page_url: str
    path: Path
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class FactSheet:
    topic: Topic
    summary: str
    facts: tuple[Fact, ...]
    sources: tuple[Source, ...]
    unknowns: tuple[str, ...] = ()
    as_of: str = ""
    image_candidates: tuple[ImageCandidate, ...] = ()
    images: tuple[QuoteImage, ...] = ()  # 확인을 통과한 인용 이미지
    engine: str = ""  # 조사 방식 (구글 딥리서치 / 구글 검색)

    def source(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def as_prompt(self, include_images: bool = False) -> str:
        """원고·검증 프롬프트에 넣는 형태 (원고 작성 때만 인용 이미지 목록을 함께 준다)."""
        sources = "\n".join(
            f"[{s.id}] {s.publisher} — {s.title} ({s.published or '날짜 미상'}){' [공식]' if s.official else ''}\n     {s.url}"
            for s in self.sources
        )
        facts = "\n".join(f"- {f.statement} ({', '.join(f.source_ids)})" for f in self.facts)
        unknowns = "\n".join(f"- {u}" for u in self.unknowns) or "- (없음)"
        text = (f"[주제] {self.topic.title}\n[요약] {self.summary}\n\n[확인된 사실]\n{facts}\n\n"
                f"[아직 공개되지 않았거나 확인되지 않은 것]\n{unknowns}\n\n[출처]\n{sources}")
        if include_images:
            listing = "\n".join(f"[{i.id}] {i.caption} (출처: {i.credit})" for i in self.images) or "- (없음)"
            text += f"\n\n[인용 이미지 — 출처 표기와 함께 본문에 넣을 수 있는 이미지]\n{listing}"
        return text

    def markdown(self) -> str:
        """슬랙 보고용 출처·사실 목록."""
        lines = [f"# 자료 조사 — {self.topic.title}", "", f"기준일: {self.as_of}", "", "## 출처"]
        lines += [f"- [{s.id}] {'(공식) ' if s.official else ''}{s.publisher} — {s.title}\n  {s.url}"
                  for s in self.sources]
        lines += ["", "## 확인된 사실"] + [f"- {f.statement} ({', '.join(f.source_ids)})" for f in self.facts]
        if self.unknowns:
            lines += ["", "## 미확인·미공개"] + [f"- {u}" for u in self.unknowns]
        if self.image_candidates:
            kept = {i.id for i in self.images}
            lines += ["", "## 인용 이미지 (딥리서치 제안)"] + [
                f"- [{c.id}] {'✅ 사용 가능' if c.id in kept else '❌ 확인 실패'} {c.caption} — {c.credit or '출처 미상'}"
                f"\n  이미지: {c.image_url}\n  페이지: {c.page_url}" for c in self.image_candidates
            ]
        if self.engine:
            lines += ["", f"조사 방식: {self.engine}"]
        return "\n".join(lines) + "\n"


def build_research_prompt(topic: Topic, today: str) -> str:
    start = f"\n출발 자료: {topic.source_url} ({topic.source_name})" if topic.source_url else ""
    return f"""당신은 AI 전문 기자입니다. 오늘은 {today} 입니다. 아래 주제로 블로그 글을 쓰기 전에 사실만 조사합니다.
주제: {topic.title}
관점: {topic.angle or '핵심 내용·스펙·가격·사용법·한계 총정리'}{start}

[조사 규칙]
- 반드시 웹 검색으로 확인합니다. 공식 발표·공식 문서·논문·저장소(1차 자료)를 가장 먼저 찾고, 신뢰할 수 있는 언론 보도로 교차 확인합니다.
- 수치(파라미터·가격·벤치마크·컨텍스트 길이·날짜)는 출처에 적힌 그대로 옮깁니다. 계산·추정·반올림하지 않습니다.
- 출처에서 확인되지 않은 것은 facts 에 넣지 말고 unknowns 에 "공개되지 않음/확인 불가"로 적습니다.
- 소문·유출·익명 게시물은 사실로 쓰지 않습니다.
- 사실은 15~30개, 출처는 4~10개. 각 사실에는 그 사실이 실제로 적힌 출처 번호를 붙입니다.

[출력] JSON 객체 하나만 (설명·코드블록 금지):
{{"summary": "주제 요약 2~3문장",
  "sources": [{{"id": "S1", "title": "문서 제목", "url": "https://...", "publisher": "발행 주체(회사·매체명)",
               "published": "YYYY-MM-DD 또는 빈 문자열", "official": true}}],
  "facts": [{{"statement": "확인된 사실 한 문장", "source_ids": ["S1"]}}],
  "unknowns": ["아직 공개되지 않은 것"]}}"""


JSON_SPEC = """{"summary": "주제 요약 2~3문장",
 "sources": [{"id": "S1", "title": "문서 제목", "url": "실제로 읽은 페이지의 원래 주소(https://...)",
              "publisher": "발행 주체(회사·매체명)", "published": "YYYY-MM-DD 또는 빈 문자열", "official": true}],
 "facts": [{"statement": "확인된 사실 한 문장", "source_ids": ["S1"]}],
 "unknowns": ["아직 공개되지 않았거나 확인되지 않은 것"],
 "images": [{"id": "I1", "image_url": "이미지 파일 직접 주소", "page_url": "이미지가 실린 페이지 주소",
             "caption": "이미지 설명 한 줄 (한국어)", "credit": "저작권자(회사·매체명)"}]}"""


def build_deep_research_prompt(topic: Topic, today: str) -> str:
    start = f"\n출발 자료: {topic.source_url} ({topic.source_name})" if topic.source_url else ""
    return f"""당신은 한국어 AI 전문 블로그의 리서처입니다. 오늘은 {today} 입니다. 블로그 글을 쓰기 전에 사실만 조사합니다.
주제: {topic.title}
관점: {topic.angle or '핵심 내용·스펙·가격·사용법·한계 총정리'}{start}

[조사 규칙]
- 공식 발표·공식 문서·논문·저장소(1차 자료)를 가장 먼저 찾고, 신뢰할 수 있는 언론 보도로 교차 확인합니다.
- 수치(파라미터·가격·벤치마크·컨텍스트 길이·날짜)는 출처에 적힌 그대로 옮깁니다. 계산·추정·반올림하지 않습니다.
- 출처에서 확인되지 않은 것은 사실에 넣지 말고 unknowns 에 "공개되지 않음/확인 불가"로 적습니다.
- 소문·유출·익명 게시물은 사실로 쓰지 않습니다.
- 사실은 15~30개, 출처는 4~10개. 각 사실에는 그 사실이 실제로 적힌 출처 번호를 붙입니다.
- 출처 url 은 검색 결과 경유 주소가 아니라 실제로 읽은 페이지의 원래 주소를 적습니다.

[인용 이미지]
- 독자가 글을 이해하는 데 꼭 필요한 공식 이미지(제품 사진, 구조도, 공식 벤치마크 도표, 논문 그림)가 출처 페이지에 있으면
  최대 3개까지 images 에 적습니다. image_url 은 실제 이미지 파일 주소, page_url 은 그 이미지가 실린 출처 페이지입니다.
- 장식용·스톡 사진·로고·아이콘·사람 얼굴 위주 사진은 넣지 않습니다. 필요 없는 주제면 images 는 빈 배열입니다.

[출력]
보고서를 쓴 뒤, 맨 끝에 아래 형식의 JSON 을 ```json 코드블록 하나로 붙입니다 (보고서와 같은 내용, 한국어).
{JSON_SPEC}"""


def build_transcribe_prompt(report: str) -> str:
    return f"""아래는 구글 딥리서치가 쓴 조사 보고서입니다. 보고서에 적힌 내용만 아래 JSON 형식으로 옮겨 적으세요.
- 보고서에 없는 사실·주소를 추가하지 않습니다. 수치는 보고서 그대로 옮깁니다.
- 출처는 보고서가 인용한 문서(주소가 있는 것)만, 사실마다 근거 출처 번호를 붙입니다.
- 보고서에 이미지 주소가 없으면 images 는 빈 배열입니다.

[출력] JSON 객체 하나만 (설명·코드블록 금지):
{JSON_SPEC}

[보고서]
{report[:60000]}"""


def extract_report_json(text: str) -> dict:
    """보고서 끝의 ```json 블록 (여러 개면 마지막 것) — 없으면 본문 전체에서 JSON 을 찾는다."""
    blocks = _JSON_BLOCKS.findall(text)
    return load_json_object(blocks[-1] if blocks else text)


def _raw_sources(data: dict) -> list[dict]:
    items = data.get("sources") or []
    return [s for s in items if isinstance(s, dict) and is_http_url(str(s.get("url") or ""))]


def _raw_facts(data: dict) -> list[Fact]:
    facts = []
    for item in data.get("facts") or []:
        if not isinstance(item, dict):
            continue
        statement = str(item.get("statement") or "").strip()
        ids = tuple(str(i).strip().upper() for i in item.get("source_ids") or [] if str(i).strip())
        if statement and ids:
            facts.append(Fact(statement, ids))
    return facts


def _validated(data: dict) -> dict:
    if not _raw_facts(data) or not _raw_sources(data):
        raise ContentError("조사 결과에 사실·출처가 없습니다")
    return data


def _parse_research(text: str) -> dict:
    return _validated(load_json_object(text))


class Researcher:
    def __init__(
        self,
        gemini: Gemini,
        session=None,
        min_sources: int = 3,
        checker: Callable[..., LinkCheck] = check_link,
        resolver: Callable[..., str] = resolve_grounding_url,
        deep: DeepResearch | None = None,
    ) -> None:
        self._gemini = gemini
        self._session = session
        self._min_sources = min_sources
        self._check = checker
        self._resolve = resolver
        self._deep = deep

    def research(self, topic: Topic, today: str, progress: Callable[[str], None] | None = None) -> FactSheet:
        if self._deep is not None:
            try:
                data, cited = self._deep_research(topic, today, progress)
                return self._build(data, cited, topic, today, ENGINE_DEEP)
            except Exception as exc:
                log.warning("구글 딥리서치 실패 — 구글 검색 조사로 대체합니다: %s", str(exc)[:300])
                if progress is not None:
                    progress(f"딥리서치 실패({str(exc)[:60]}) → 구글 검색 조사로 대체")
        data, cited = self._search_research(topic, today)
        return self._build(data, cited, topic, today, ENGINE_SEARCH if self._deep is not None else "구글 검색")

    def _deep_research(self, topic: Topic, today: str, progress) -> tuple[dict, list[str]]:
        report = self._deep.run(build_deep_research_prompt(topic, today), progress)
        try:
            data = _validated(extract_report_json(report.text))
        except ContentError as exc:
            log.info("딥리서치 JSON 을 바로 읽지 못해 보고서를 옮겨 적습니다: %s", exc)
            data, _ = self._gemini.generate(build_transcribe_prompt(report.text), _parse_research,
                                            temperature=0.1, label="딥리서치 보고서 정리")
        cited = [self._resolve(url, self._session) for url in report.cited_urls[:MAX_CITED]]
        return data, cited

    def _search_research(self, topic: Topic, today: str) -> tuple[dict, list[str]]:
        data, reply = self._gemini.generate(
            build_research_prompt(topic, today), _parse_research, grounded=True, url_context=bool(topic.source_url),
            temperature=0.2, label="자료 조사",
        )
        log.info("구글 검색 조사: 검색어 %s", ", ".join(reply.queries))
        return data, [self._resolve(s.url, self._session) for s in reply.sources]

    def _build(self, data: dict, cited: Sequence[str], topic: Topic, today: str, engine: str) -> FactSheet:
        from .quote_images import parse_candidates

        sources = self._verified_sources(data, cited, topic)
        valid_ids = {s.id for s in sources}
        facts = tuple(
            replace(f, source_ids=tuple(i for i in f.source_ids if i in valid_ids))
            for f in _raw_facts(data) if any(i in valid_ids for i in f.source_ids)
        )
        used = {i for f in facts for i in f.source_ids}
        sources = tuple(s for s in sources if s.id in used)
        log.info("자료 조사(%s): 사실 %d개 · 확인된 출처 %d개", engine, len(facts), len(sources))
        if len(facts) < MIN_FACTS or len(sources) < self._min_sources:
            raise ResearchError(
                f"확인된 자료가 부족합니다 (사실 {len(facts)}개/최소 {MIN_FACTS}, 출처 {len(sources)}개/최소 {self._min_sources}). "
                "주제가 너무 새롭거나 공식 자료가 아직 없을 수 있습니다."
            )
        unknowns = tuple(str(u).strip() for u in data.get("unknowns") or [] if str(u).strip())
        return FactSheet(topic, str(data.get("summary") or "").strip(), facts, sources, unknowns, today,
                         image_candidates=parse_candidates(data.get("images")), engine=engine)

    def _verified_sources(self, data: dict, cited: Sequence[str], topic: Topic) -> tuple[Source, ...]:
        grounded_keys = {canonical(u) for u in unique_urls(cited)}
        keywords = list(topic.keywords)
        verified: list[Source] = []
        for raw in _raw_sources(data)[:MAX_SOURCES]:
            url = self._resolve(str(raw["url"]).strip(), self._session)
            check = self._check(url, keywords, self._session)
            in_grounding = canonical(url) in grounded_keys or canonical(check.url) in grounded_keys
            accepted = (check.alive and (check.mentions or in_grounding)) or (check.blocked and in_grounding)
            if not accepted:
                log.info("출처 제외 (%s, 상태 %s, 주제어 %s): %s", "인용 확인" if in_grounding else "미확인",
                         check.status, check.mentions, url)
                continue
            final_url = check.url if check.alive else url
            verified.append(Source(
                id=str(raw.get("id") or f"S{len(verified) + 1}").strip().upper(),
                title=str(raw.get("title") or check.title or domain(final_url)).strip(),
                url=final_url,
                publisher=str(raw.get("publisher") or domain(final_url)).strip(),
                published=str(raw.get("published") or "").strip()[:10],
                official=bool(raw.get("official")),
                grounded=in_grounding,
            ))
        return tuple(verified)


def reference_date(published: str, fallback_year: str) -> str:
    """'2026-09-26' → '2026, September 26' (APA 표기). 날짜를 모르면 연도만."""
    months = ("January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December")
    parts = published.split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts) and 1 <= int(parts[1]) <= 12:
        return f"{parts[0]}, {months[int(parts[1]) - 1]} {int(parts[2])}"
    return parts[0] if parts and parts[0].isdigit() and len(parts[0]) == 4 else fallback_year


def pick_sources(sheet: FactSheet, cited: Sequence[str]) -> tuple[Source, ...]:
    """원고가 인용한 출처 (없으면 전체) — 공식 자료 먼저."""
    chosen = [s for s in sheet.sources if s.id in set(cited)] or list(sheet.sources)
    return tuple(sorted(chosen, key=lambda s: not s.official))
