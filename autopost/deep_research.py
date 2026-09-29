"""구글 딥리서치 (Gemini Deep Research 에이전트, Interactions API) 호출.

에이전트가 스스로 검색 계획을 세우고 수십 개 페이지를 읽은 뒤 인용이 달린 보고서를 쓴다 (보통 5~20분).
백그라운드로 시작하고 끝날 때까지 주기적으로 상태를 확인한다. 비용은 조사 1건에 약 1~3달러 (Gemini API 유료 요금).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .links import is_http_url, unique_urls

log = logging.getLogger(__name__)

ACTIVE = {"in_progress", "queued", "pending", "running", "created"}
POLL_SECONDS = 20
PROGRESS_EVERY_SECONDS = 120
MAX_POLL_ERRORS = 6
URL_KEYS = {"url", "uri", "source_url", "page_url"}


class DeepResearchError(RuntimeError):
    """딥리서치가 실패했거나 시간 안에 끝나지 않음."""


@dataclass(frozen=True)
class DeepResearchReport:
    text: str
    cited_urls: tuple[str, ...]  # 에이전트가 실제로 인용한 주소 (검색 근거 경유 주소 포함)
    interaction_id: str
    minutes: float


def _plain(obj):
    """SDK 객체 → dict/list (응답 구조가 버전마다 조금씩 달라 필드 이름에 기대지 않고 훑는다)."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    return obj


def _walk_urls(node, in_text: bool = False) -> Iterable[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in URL_KEYS and isinstance(value, str) and is_http_url(value):
                yield value
            elif key != "text":  # 본문 글자 속 주소는 인용이 아니다
                yield from _walk_urls(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_urls(item)


def _outputs(data: dict) -> list:
    """최종 출력 목록 — 최신 SDK 는 steps[type=model_output].content, 예전 형식은 outputs."""
    steps = [s for s in data.get("steps") or [] if isinstance(s, dict) and s.get("type") in (None, "model_output")]
    if steps:
        return [item for step in steps for item in step.get("content") or []]
    return list(data.get("outputs") or [])


def report_text(data: dict) -> str:
    texts = [str(item.get("text") or "") for item in _outputs(data)
             if isinstance(item, dict) and item.get("type") in (None, "text") and item.get("text")]
    return "\n".join(texts).strip() or str(data.get("output_text") or "").strip()


def cited_urls(data: dict) -> tuple[str, ...]:
    return tuple(unique_urls(_walk_urls(_outputs(data))))


class DeepResearch:
    def __init__(
        self,
        api_key: str,
        agent: str,
        client=None,
        timeout_minutes: int = 45,
        poll_seconds: float = POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)
        self._client = client
        self._agent = agent
        self._timeout = timeout_minutes * 60
        self._poll = poll_seconds
        self._sleep = sleep
        self._clock = clock

    @property
    def agent(self) -> str:
        return self._agent

    def run(self, prompt: str, progress: Callable[[str], None] | None = None) -> DeepResearchReport:
        interaction = self._client.interactions.create(
            input=prompt, agent=self._agent, background=True,
            agent_config={"type": "deep-research", "thinking_summaries": "none", "visualization": "off"},
        )
        interaction_id = str(getattr(interaction, "id", "") or "")
        log.info("구글 딥리서치 시작 (%s): %s", self._agent, interaction_id)
        start = last_note = self._clock()
        errors = 0
        while str(getattr(interaction, "status", "")) in ACTIVE:
            elapsed = self._clock() - start
            if elapsed > self._timeout:
                self._cancel(interaction_id)
                raise DeepResearchError(f"구글 딥리서치가 {self._timeout // 60}분 안에 끝나지 않았습니다")
            self._sleep(self._poll)
            try:
                interaction = self._client.interactions.get(interaction_id)
                errors = 0
            except Exception as exc:  # 일시적 네트워크 오류는 다음 확인에서 다시
                errors += 1
                log.warning("딥리서치 상태 확인 실패 (%d/%d): %s", errors, MAX_POLL_ERRORS, str(exc)[:200])
                if errors >= MAX_POLL_ERRORS:
                    raise DeepResearchError(f"딥리서치 상태를 확인할 수 없습니다: {exc}") from exc
            if progress is not None and self._clock() - last_note >= PROGRESS_EVERY_SECONDS:
                last_note = self._clock()
                progress(f"구글 딥리서치 조사 중 — {int((last_note - start) // 60)}분 경과")
        minutes = (self._clock() - start) / 60
        status = str(getattr(interaction, "status", ""))
        if status != "completed":
            detail = getattr(interaction, "error", None) or status
            raise DeepResearchError(f"구글 딥리서치 실패 ({status}): {str(detail)[:300]}")
        data = _plain(interaction)
        text = report_text(data) if isinstance(data, dict) else ""
        if not text:
            raise DeepResearchError("구글 딥리서치 보고서가 비어 있습니다")
        urls = cited_urls(data) if isinstance(data, dict) else ()
        log.info("구글 딥리서치 완료: %.1f분 · 보고서 %d자 · 인용 주소 %d개", minutes, len(text), len(urls))
        return DeepResearchReport(text, urls, interaction_id, minutes)

    def _cancel(self, interaction_id: str) -> None:
        cancel = getattr(self._client.interactions, "cancel", None)
        if cancel is None:
            return
        try:
            cancel(interaction_id)
        except Exception as exc:
            log.info("딥리서치 취소 요청 실패: %s", exc)
