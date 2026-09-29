"""GitHub Actions 실행 요약 페이지(Job Summary) 작성 — Actions 화면에서 결과를 한눈에 보도록."""
from __future__ import annotations

import logging
import os
from typing import Mapping, Sequence

from .results import PublishResult

log = logging.getLogger(__name__)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def build_step_summary(
    title: str,
    category: str,
    topic_url: str,
    chars: int,
    fact_line: str,
    results: Sequence[PublishResult],
    dry_run: bool,
    held: bool = False,
) -> str:
    head = "⚠️ 사실 검증 미통과 — 발행 보류" if held else ("🧪 테스트 실행 (발행 안 함)" if dry_run else "🤖 포스팅 결과")
    lines = [
        f"## {head}",
        "",
        f"- **제목**: {_cell(title)}",
        f"- **카테고리**: {_cell(category)}",
        f"- **원문**: {topic_url or '없음'}",
        f"- **본문**: {chars:,}자",
        f"- **사실 검증**: {_cell(fact_line)}",
        "",
        "| 플랫폼 | 결과 | 링크 | 메모 |",
        "|---|---|---|---|",
    ]
    for r in results:
        status = "⏭️ 건너뜀" if r.skipped else ("✅ 성공" if r.ok else "❌ 실패")
        link = f"[열기]({r.url})" if r.url else ""
        lines.append(f"| {r.platform} | {status} | {link} | {_cell(r.message)[:120]} |")
    lines += ["", "출처 목록·원고·사실 검증 보고는 슬랙 채널에서 확인할 수 있습니다."]
    return "\n".join(lines) + "\n"


def build_failure_summary(label: str, step_label: str, error: Exception | str) -> str:
    """도중에 멈춘 실행도 Actions 요약 페이지에서 어느 단계가 왜 실패했는지 보이도록."""
    detail = str(error).replace("```", "'''")[:1500]
    return (
        f"## ❌ 포스팅 실패 — {label}\n\n"
        f"- **실패 단계**: {step_label}\n"
        f"- 발행 기록을 남기지 않았으므로 다음 실행에서 다시 주제를 고릅니다.\n\n"
        f"```\n{detail}\n```\n"
    )


def write_step_summary(markdown: str, env: Mapping[str, str] | None = None) -> bool:
    path = (os.environ if env is None else env).get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return False
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
        return True
    except OSError as exc:
        log.warning("실행 요약 작성 실패: %s", exc)
        return False
