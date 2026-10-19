"""Evidence manifest for added reader value, separate from fact checking.

The static check validates traceability, not the semantic truth or originality of prose.
2026-10-20 in-flight drafts remain compatible; the daily cutoff is KST 2026-10-21.
"""
from __future__ import annotations

import re
from .content import PostContent

STANDARD_VERSION = 1
REQUIRED_FROM_DAY = "20261021"
KINDS = {"comparison", "calculation", "decision_guide", "source_synthesis"}

EDITORIAL_RULES = """[독자 가치·독창성 기준 v1]
- 먼저 이 글만으로 답할 독자의 구체적인 질문 1개를 정하고, 제목은 그 질문·변화·판단 기준을 드러냅니다.
  모든 제목에 '총정리/완벽/사용 가이드'를 붙이거나 다른 블로그의 문장·구성을 복제하지 않습니다.
- 단일 보도자료의 번역·바꿔쓰기로 완성하지 않습니다. 서로 다른 소제목에 최소 두 종류의 독자 가치를 만듭니다:
  comparison(같은 조건의 비교), calculation(출처 수치·단위·가정·식이 있는 계산),
  decision_guide(독자의 상황별 선택 기준과 제외 조건), source_synthesis(서로 다른 원문 2개 이상을 연결한 해석).
  근거에 맞는 종류만 선택합니다. 비교 조건이 다르면 우열을 단정하지 않고 차이를 명시합니다.
- 새 외부 사실은 공식 원문으로 확인합니다. 해석·예시·가정 계산은 그렇게 표시하고 근거와 한계를 붙입니다.
  직접 실행한 증거가 없으면 '써보니/테스트 결과/실측'을 쓰지 않습니다. 가짜 경험·사용자 반응·인간 검토를 만들지 않습니다.
- 분량을 채우려고 요약·표·FAQ에 같은 문장을 반복하지 않습니다. 표마다 다른 판단 목적을 두고 FAQ도 본문을 복제하지 않습니다.
  자료가 부족하면 추가 근거를 조사합니다. 근거와 독자 가치를 확보하지 못하면 적격 후보 교체/실패 절차를 따릅니다.
- research.json에 editorial 객체를 저장합니다: standard_version:1, reader_question, limitations,
  contributions:[{kind,section,excerpt,source_ids,added_value}].
  최소 두 종류·두 소제목. section은 본문의 정확한 소제목, excerpt는 그 소제목의 paragraph/info/warning/list/table 블록에서
  독자 가치가 드러나는 연속된 원문 30자 이상을 그대로 복사합니다. source_ids는 실제 research.sources의 ID,
  added_value는 원문 요약에서 더한 구체적인 판단 도움입니다. source_synthesis는 서로 다른 URL 2개 이상이 필요합니다.
- 별도 검증자는 원고와 원문을 다시 읽어 위 기여가 실제 추가 분석인지, 비교·계산 조건과 한계가 정확한지 점검합니다.
  형식 검사를 통과했다는 이유로 독창성이 입증됐다고 쓰지 않습니다. 부족하면 본문과 editorial을 함께 수정하고,
  해결할 근거가 없으면 verification_failed로 처리합니다. 사실 검증·최신성·중복·승인 기준은 그대로 유지합니다.
"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text.replace("**", ""))


def editorial_problems(content: PostContent, research: dict, day: str) -> list[str]:
    editorial = research.get("editorial")
    if editorial is None and (not day or day < REQUIRED_FROM_DAY):
        return []  # Already-created legacy drafts keep their existing approval path.
    if not isinstance(editorial, dict) or type(editorial.get("standard_version")) is not int or editorial["standard_version"] != STANDARD_VERSION:
        return ["독자 가치 기록 editorial.standard_version:1이 필요합니다"]
    problems: list[str] = []
    for key in ("reader_question", "limitations"):
        if not isinstance(editorial.get(key), str) or not editorial[key].strip():
            problems.append(f"editorial.{key}가 비어 있습니다")
    items = editorial.get("contributions")
    if not isinstance(items, list):
        return problems + ["editorial.contributions가 배열이 아닙니다"]
    sources = {str(s.get("id", "")).upper(): str(s.get("url", "")).split("#")[0].rstrip("/")
               for s in research.get("sources", []) if isinstance(s, dict)}
    sections = {s.heading: _norm(" ".join(t for b in s.blocks if b.kind in {"paragraph", "info", "warning", "list", "table"} for t in b.texts()))
                for s in content.sections}
    valid = []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            problems.append(f"독자 가치 {i}: 객체가 아닙니다")
            continue
        kind, section = item.get("kind"), item.get("section")
        excerpt = _norm(item.get("excerpt", "")) if isinstance(item.get("excerpt"), str) else ""
        ids = item.get("source_ids")
        known = isinstance(ids, list) and bool(ids) and all(isinstance(x, str) and x.upper() in sources for x in ids)
        reason = item.get("added_value")
        if not isinstance(kind, str) or kind not in KINDS or not isinstance(section, str) or section not in sections or len(excerpt) < 30 or excerpt not in sections.get(section, ""):
            problems.append(f"독자 가치 {i}: 종류·소제목·본문 발췌를 확인하세요")
        elif not known or not isinstance(reason, str) or not reason.strip():
            problems.append(f"독자 가치 {i}: 출처 ID·추가 가치를 확인하세요")
        elif kind == "source_synthesis" and len({sources[x.upper()] for x in ids}) < 2:
            problems.append(f"독자 가치 {i}: 종합 해석은 서로 다른 출처 URL 2개 이상이 필요합니다")
        else:
            valid.append((kind, section, excerpt))
    if len({x[0] for x in valid}) < 2 or len({x[1] for x in valid}) < 2 or len({x[2] for x in valid}) < 2:
        problems.append("서로 다른 종류·소제목·발췌의 독자 가치가 최소 2개 필요합니다")
    return problems
