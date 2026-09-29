"""Gemini 호출 공용 래퍼 — 모델 대체(fallback)·재시도·구글 검색 근거(grounding) 추출.

grounded=True 이면 구글 검색 도구를 켜고, 모델이 실제로 참고한 웹 문서(제목·주소)를 함께 돌려준다.
이 목록이 '사실 확인'의 근거가 된다 — 모델이 JSON 에 적은 주소는 지어낸 것일 수 있어 따로 검증한다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Sequence, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
RETRY_WAIT_SECONDS = 15  # 모든 모델이 실패한 라운드 뒤 대기 (라운드마다 늘어남)


class GeminiError(RuntimeError):
    """모든 모델·재시도가 실패."""


@dataclass(frozen=True)
class WebSource:
    title: str
    url: str


@dataclass(frozen=True)
class GeminiReply:
    text: str
    model: str
    sources: tuple[WebSource, ...] = ()
    queries: tuple[str, ...] = ()


def extract_grounding(response) -> tuple[tuple[WebSource, ...], tuple[str, ...]]:
    """응답의 grounding_metadata 에서 웹 출처와 검색어를 꺼낸다 (없으면 빈 값)."""
    candidates = getattr(response, "candidates", None) or []
    metadata = getattr(candidates[0], "grounding_metadata", None) if candidates else None
    if metadata is None:
        return (), ()
    sources: list[WebSource] = []
    seen: set[str] = set()
    for chunk in getattr(metadata, "grounding_chunks", None) or []:
        web = getattr(chunk, "web", None)
        uri = getattr(web, "uri", "") if web is not None else ""
        if uri and uri not in seen:
            seen.add(uri)
            sources.append(WebSource(title=getattr(web, "title", "") or "", url=uri))
    queries = tuple(q for q in getattr(metadata, "web_search_queries", None) or [] if q)
    return tuple(sources), queries


class Gemini:
    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_models: Sequence[str] = (),
        client=None,
        max_rounds: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)
        self._client = client
        self._models = tuple(dict.fromkeys((model, *fallback_models)))  # 순서 유지 중복 제거
        self._max_rounds = max_rounds
        self._sleep = sleep

    @staticmethod
    def _config(grounded: bool, url_context: bool, json_mode: bool, temperature: float):
        from google.genai import types

        tools = []
        if grounded:
            tools.append(types.Tool(google_search=types.GoogleSearch()))
        if url_context:
            tools.append(types.Tool(url_context=types.UrlContext()))
        return types.GenerateContentConfig(
            temperature=temperature,
            tools=tools or None,
            # 검색 도구와 JSON 강제 출력은 함께 쓸 수 없는 모델이 있어, 도구를 쓸 때는 프롬프트로 JSON 을 요구한다
            response_mime_type="application/json" if json_mode and not tools else None,
        )

    def generate(
        self,
        contents,
        parse: Callable[[str], T],
        *,
        grounded: bool = False,
        url_context: bool = False,
        json_mode: bool = True,
        temperature: float = 0.4,
        label: str = "AI 호출",
    ) -> tuple[T, GeminiReply]:
        """한 라운드에 모든 모델을 순서대로 시도 → 전부 실패하면 대기 후 다음 라운드.

        parse 가 예외를 내면(형식 오류) 그 응답은 실패로 보고 다음 모델로 넘어간다.
        """
        config = self._config(grounded, url_context, json_mode, temperature)
        last_error: Exception | None = None
        for round_no in range(1, self._max_rounds + 1):
            for model in self._models:
                try:
                    response = self._client.models.generate_content(model=model, contents=contents, config=config)
                    text = response.text or ""
                    parsed = parse(text)
                except Exception as exc:  # 과부하/모델중단/네트워크/파싱 오류 모두 다음 후보로
                    last_error = exc
                    log.warning("%s 실패 [%s] (라운드 %d/%d): %s", label, model, round_no, self._max_rounds, exc)
                    continue
                sources, queries = extract_grounding(response)
                return parsed, GeminiReply(text=text, model=model, sources=sources, queries=queries)
            if round_no < self._max_rounds:
                self._sleep(RETRY_WAIT_SECONDS * round_no)  # 과부하(503)가 풀리도록 15·30초 대기
        total = self._max_rounds * len(self._models)
        raise GeminiError(f"{label}에 {total}회 실패했습니다: {last_error}")
