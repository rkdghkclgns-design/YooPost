"""AI 원고 작성·수정 — 참고 블로그와 같은 문법으로, 조사된 사실(fact sheet)만 사용한다."""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Sequence

from .constants import CATEGORIES, CLOSING_SIGNATURE, IPO_DISCLAIMER
from .content import PostContent, parse_post
from .gemini import Gemini
from .research import FactSheet
from .style_profile import StyleGuide

log = logging.getLogger(__name__)

IPO_CATEGORY = "AI 공모주 분석"

OUTPUT_SCHEMA = """{
  "title": "이모지 1개 + 핵심 제품/모델명 + 핵심 수치·혜택 + '사용 가이드/총정리' 식 (60자 안팎)",
  "category": "카테고리",
  "intro": "안녕하세요! 로 시작하는 도입 문단 (4~6문장)",
  "summary": ["한눈에 보기 항목 5~7개 (마지막 항목은 '이 글에서 배울 것: ...')"],
  "sections": [
    {"heading": "이모지 + 소제목",
     "blocks": [
       {"type": "paragraph", "text": "문단 — 핵심 수치·결론·고유명사는 **강조** 표시"},
       {"type": "subheading", "text": "h3 소제목"},
       {"type": "info", "title": "💡 박스 제목", "text": "핵심 팁·해설"},
       {"type": "warning", "title": "⚠️ 박스 제목", "text": "주의할 점"},
       {"type": "table", "headers": ["항목", "내용"], "rows": [["...", "..."]]},
       {"type": "code", "language": "bash|python|json|plaintext", "code": "코드 또는 복사용 프롬프트"},
       {"type": "list", "items": ["...", "..."]},
       {"type": "image", "image_id": "I1"}
     ]}
  ],
  "faq": [{"q": "질문", "a": "답변 2~4문장"}],
  "closing": "지금까지 ...에 대해 알아보았습니다. 로 시작하는 맺음말 (3~5문장)",
  "caveat": "작성 기준일 뒤에 붙일 주의 문구 한 문장 (예: 가격·무료 기간은 바뀔 수 있으니 공식 문서에서 최신 정보를 확인하세요)",
  "hashtags": ["해시태그 10~15개, # 없이, 띄어쓰기 없이"],
  "source_ids": ["본문에서 사용한 출처 번호 S1, S2 ..."]
}"""


def style_rules(style: StyleGuide, blog_name: str) -> str:
    headings = "\n".join(f"  - {h}" for h in style.headings)
    emojis = " ".join(style.title_emojis)
    based = f"참고 블로그 전체 {style.total_posts}개 게시물 분석 결과" if style.total_posts else "참고 블로그 분석 결과"
    return f"""[글 문법 — {based}를 그대로 따릅니다]
1. 제목: 맨 앞에 이모지 1개({emojis} 등) + 제품·모델명 + 핵심 수치나 혜택 + '사용 가이드', '총정리' 같은 말.
   무료로 쓸 수 있는 소식이면 🆓, 새 모델이면 🤖, 속도·성능이면 ⚡, 보안이면 🔒 를 주로 씁니다.
   예) "🤖 ○○ 사용 가이드: 1M 컨텍스트·가격·벤치마크 총정리"
2. intro: "안녕하세요! {{날짜}}, {{회사}}가 {{제품}}을 공개했습니다." 처럼 언제·누가·무엇을 발표했는지로 시작해
   왜 주목할 만한지, 이 글에서 무엇을 다루는지 소개하고 "함께 살펴보시죠!" 식으로 끝냅니다.
   발표 날짜는 [확인된 사실]에 적힌 날짜만 씁니다. 사실 목록에 발표일이 없으면 날짜를 빼고 "최근"처럼 쓰며,
   오늘 날짜(작성일)를 발표일처럼 쓰지 않습니다.
3. summary: '한눈에 보기' 박스에 들어갈 핵심 요약 5~7줄. 마지막 줄은 "이 글에서 배울 것: ...".
4. sections: 이모지로 시작하는 h2 소제목 {max(5, style.h2_count - 4)}~{max(7, style.h2_count - 2)}개. 참고 블로그에서 자주 쓰인 소제목 패턴:
{headings}
   주제에 맞는 것을 골라 '○○ 등장 배경', '○○ 공식 스펙 정리'처럼 주제어를 넣어 씁니다.
   (❓ 자주 묻는 질문 · 🔗 함께 보면 좋은 글 · 🎯 맺음말 · 📚 참고 문헌은 시스템이 따로 붙이므로 sections 에 넣지 마세요)
5. 각 섹션: 문단 2~4개 + 필요한 곳에 h3(subheading). 글 전체에 💡info 박스 {max(3, style.info_boxes)}개 안팎,
   ⚠️warning 박스 {max(1, style.warning_boxes)}~{max(2, style.warning_boxes + 1)}개, 스펙·가격·비교 표 {max(2, style.tables)}개 안팎을 씁니다.
6. 코드블록: 출처에 나온 설치 명령·설정·API 호출 예시, 또는 독자가 AI 에이전트나 모델에 그대로 붙여넣는
   '복사용 지시문/예제 프롬프트'에만 씁니다 (참고 블로그의 '에이전트에게 붙여넣기' 형식). 출처에 없는 명령·옵션·모델 ID를 지어내지 않습니다.
7. faq: 독자가 실제로 궁금해할 질문 {max(4, style.faq_questions)}개 안팎. 답은 조사된 사실로만.
8. closing: "지금까지 ○○에 대해 알아보았습니다."로 시작해 핵심을 정리하고, 남은 불확실성을 짚은 뒤
   "여러분도 한번 ○○을 직접 체험해보시길 추천드리면서 {CLOSING_SIGNATURE}" 로 끝냅니다.
9. 문체: 존댓말(~습니다/~입니다), 친근하지만 정확하게. 전문용어는 처음 나올 때 괄호로 풀어 씁니다 (예: MoE(전문가 혼합)).
   블로그 이름은 '{blog_name}'.
10. 강조: 참고 블로그처럼 문단마다 독자가 꼭 기억해야 할 핵심 1~3곳을 **강조**로 감쌉니다 (굵은 글씨 + 형광 배경으로 보임).
   대상은 핵심 수치("**88개 커스텀 코어**"), 결론 한 구절("**에이전트 전용 CPU**"), 처음 소개하는 제품·기능 이름.
   문장 전체나 한 문단에 4곳 이상은 강조하지 않습니다. 소제목·표 머리글에는 쓰지 않습니다.
11. 인용 이미지: 조사 자료에 [인용 이미지]가 있으면, 내용이 맞는 섹션의 관련 문단 바로 뒤에
   {{"type": "image", "image_id": "I1"}} 블록으로 한 번씩 넣습니다 (캡션·출처는 시스템이 붙입니다).
   글 내용과 관계없는 이미지는 넣지 않고, 목록이 '(없음)'이면 image 블록을 쓰지 않습니다. 목록에 없는 번호를 지어내지 않습니다."""


FACT_RULES = """[사실 규칙 — 가장 중요]
- 아래 [확인된 사실]에 있는 내용만 사실로 씁니다. 수치·날짜·가격·이름·버전은 한 글자도 바꾸지 않습니다.
- 사실 목록에 없는 수치·기능·가격·출시일·평가를 지어내지 않습니다. 모르면 "아직 공개되지 않았습니다"라고 씁니다.
- [아직 공개되지 않았거나 확인되지 않은 것]은 확정된 것처럼 쓰지 않습니다.
- 의견·전망은 "~로 보입니다", "~할 수 있습니다"처럼 해석임을 드러내고, 사실과 구분합니다.
- 커뮤니티 반응·사용 후기를 지어내지 않습니다. 확인된 자료가 없으면 그 섹션을 쓰지 않습니다.
- 최고·유일·혁명적 같은 과장 표현은 출처가 그렇게 말한 경우에만 인용 형태로 씁니다.
- 본문에서 사용한 출처 번호를 source_ids 에 모두 적습니다.
- 'AI 공모주 분석' 글은 공모가·일정·청약·수요예측·재무 수치 등 증권신고서·공시에 적힌 사실만 정리합니다.
  수익률 전망, 매수·매도 권유, '대박' 같은 표현은 쓰지 않습니다."""


def build_write_prompt(sheet: FactSheet, style: StyleGuide, blog_name: str, today: str, min_chars: int) -> str:
    topic = sheet.topic
    return f"""당신은 한국어 AI 전문 블로그 '{blog_name}'의 작가입니다. 오늘은 {today} 입니다.
아래 조사 자료로 블로그 글 1편을 씁니다. 주제: {topic.title}
관점: {topic.angle or '핵심 내용·스펙·가격·사용법·한계 총정리'} / 카테고리 후보: {topic.category}
카테고리는 {', '.join(CATEGORIES)} 중 하나.

{style_rules(style, blog_name)}

{FACT_RULES}

[분량] 본문(공백 제외, 코드블록 제외) {min_chars:,}~{style.target_chars:,}자. 짧게 끝내지 말고 배경·의미·사용법·주의점을 충분히 설명합니다.

[조사 자료]
{sheet.as_prompt(include_images=True)}

[출력] 아래 JSON 객체 하나만 출력하세요 (설명·코드블록 금지).
{OUTPUT_SCHEMA}"""


def build_revise_prompt(content_json: str, sheet: FactSheet, issues: Sequence[dict], blog_name: str) -> str:
    listing = json.dumps(list(issues), ensure_ascii=False, indent=1)
    return f"""당신은 AI 전문 블로그 '{blog_name}'의 편집자입니다. 아래 원고를 사실 검증 결과에 맞게 고칩니다.

[검증에서 문제가 된 주장]
{listing}

[고치는 방법]
- verdict 가 contradicted 인 주장: correction 과 evidence 에 맞게 고치고, 확실하지 않으면 그 문장을 지웁니다.
- verdict 가 unverifiable 인 주장: 그 주장을 지우거나, 조사 자료에 있는 사실로만 다시 씁니다.
- 문제없는 부분은 그대로 둡니다. 글의 구성(JSON 구조·소제목·박스·표·image 블록·**강조** 표시)은 유지합니다.
- 표의 칸을 지울 때는 행 전체를 지웁니다.

{FACT_RULES}

[조사 자료]
{sheet.as_prompt()}

[원고 JSON]
{content_json}

[출력] 고친 원고 전체를 같은 JSON 구조로 하나만 출력하세요 (설명·코드블록 금지)."""


def ensure_signature(closing: str) -> str:
    """맺음말 끝인사를 참고 블로그와 같게 맞춘다 (AI 가 빠뜨려도)."""
    if "다시 찾아뵙겠습니다" in closing:
        return closing
    return f"{closing.rstrip()} {CLOSING_SIGNATURE}"


def finalize(content: PostContent) -> PostContent:
    """끝인사 보정 + 공모주 글에는 투자 권유가 아니라는 안내를 항상 붙인다."""
    caveat = content.caveat
    if content.category == IPO_CATEGORY and IPO_DISCLAIMER not in caveat:
        caveat = f"{caveat} {IPO_DISCLAIMER}".strip()
    return replace(content, closing=ensure_signature(content.closing), caveat=caveat)


def drop_unknown_images(content: PostContent, image_ids: frozenset[str]) -> PostContent:
    """확인된 인용 이미지 번호만 남긴다 (AI 가 없는 번호를 지어내면 그 블록을 버린다)."""
    sections = tuple(
        replace(section, blocks=tuple(b for b in section.blocks if b.kind != "image" or b.image_id in image_ids))
        for section in content.sections
    )
    return replace(content, sections=sections)


def content_to_json(content: PostContent) -> str:
    def block(b) -> dict:
        if b.kind == "table":
            return {"type": "table", "headers": list(b.headers), "rows": [list(r) for r in b.rows]}
        if b.kind == "code":
            return {"type": "code", "language": b.language, "code": b.text}
        if b.kind == "list":
            return {"type": "list", "items": list(b.items)}
        if b.kind == "image":
            return {"type": "image", "image_id": b.image_id}
        data = {"type": b.kind, "text": b.text}
        return {**data, "title": b.title} if b.title else data

    return json.dumps({
        "title": content.title, "category": content.category, "intro": content.intro,
        "summary": list(content.summary),
        "sections": [{"heading": s.heading, "blocks": [block(b) for b in s.blocks]} for s in content.sections],
        "faq": [{"q": f.question, "a": f.answer} for f in content.faq],
        "closing": content.closing, "caveat": content.caveat,
        "hashtags": [t.lstrip("#") for t in content.hashtags], "source_ids": list(content.source_ids),
    }, ensure_ascii=False)


class PostWriter:
    def __init__(self, gemini: Gemini, blog_name: str, min_chars: int) -> None:
        self._gemini = gemini
        self._blog_name = blog_name
        self._min_chars = min_chars

    def _parse(self, category: str, image_ids: frozenset[str] = frozenset()):
        def parse(text: str) -> PostContent:
            return drop_unknown_images(finalize(parse_post(text, category)), image_ids)
        return parse

    def write(self, sheet: FactSheet, style: StyleGuide, today: str) -> PostContent:
        prompt = build_write_prompt(sheet, style, self._blog_name, today, self._min_chars)
        parse = self._parse(sheet.topic.category, frozenset(i.id for i in sheet.images))
        content, _ = self._gemini.generate(prompt, parse, temperature=0.6, label="AI 원고 작성")
        length = content.text_length()
        if length >= self._min_chars:
            return content
        log.warning("AI 원고가 짧아 재요청합니다 (%d자 < 최소 %d자)", length, self._min_chars)
        longer = prompt + (f"\n※ 직전 답변의 본문이 공백 제외 {length:,}자로 너무 짧았습니다. "
                           f"조사 자료의 사실을 더 활용해 공백 제외 {self._min_chars:,}자 이상으로 쓰세요.")
        retry, _ = self._gemini.generate(longer, parse, temperature=0.6, label="AI 원고 보강")
        return retry if retry.text_length() > length else content

    def revise(self, content: PostContent, sheet: FactSheet, issues: Sequence[dict]) -> PostContent:
        prompt = build_revise_prompt(content_to_json(content), sheet, issues, self._blog_name)
        parse = self._parse(content.category, frozenset(i.id for i in sheet.images))
        revised, _ = self._gemini.generate(prompt, parse, temperature=0.2, label="원고 사실 수정")
        return revised
