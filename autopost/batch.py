"""카테고리별 포스팅 — 블로그 메뉴의 카테고리마다 1편씩 차례로 쓴다 (슬랙 '/aipost 전체').

    AI 언어 모델 → AI 도구 → AI 논문 분석 → AI 뉴스 리뷰 → AI 보안 → AI 인사이트 → AI 공모주 분석
- 한 편씩 순서대로 진행한다 (동시에 돌리면 AI 요청 한도에 걸리고 같은 소식을 두 번 고를 수 있음).
- 앞에서 고른 주제는 뒤 카테고리에서 다시 고르지 않는다.
- 한 카테고리가 실패·보류돼도 나머지는 계속하고, 끝나면 결과를 슬랙 카드 한 장으로 모아 보고한다.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Sequence

from .config import Config
from .constants import CATEGORIES
from .gemini import Usage
from .pipeline import AlreadyReported, PostOutcome, RunOptions, Services, post_once
from .slack_helper import LABEL, escape_mrkdwn
from .step_summary import write_step_summary

log = logging.getLogger(__name__)

ALL = "all"


def _icon(outcome: PostOutcome, dry_run: bool) -> str:
    if outcome.ok:
        return "🧪" if dry_run else "✅"
    return "⏭️" if outcome.status.endswith("보류") else "❌"


def build_batch_blocks(outcomes: Sequence[PostOutcome], dry_run: bool) -> list[dict]:
    done = sum(1 for o in outcomes if o.ok)
    head = f"🧪 {LABEL} 카테고리별 테스트 결과" if dry_run else f"📚 {LABEL} 카테고리별 포스팅 결과"
    lines = []
    for o in outcomes:
        line = f"{_icon(o, dry_run)} *{escape_mrkdwn(o.category)}* — "
        line += f"<{o.url}|{escape_mrkdwn(o.title)}>" if o.url else escape_mrkdwn(o.title or o.topic or "주제 없음")
        lines.append(f"{line}\n      {escape_mrkdwn(o.status)}")
    total = sum((o.usage for o in outcomes), Usage())
    summary = f"{len(outcomes)}개 카테고리 중 {done}개 {'완료' if dry_run else '발행'}"
    return [
        {"type": "header", "text": {"type": "plain_text", "text": head}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"📊 합계 {escape_mrkdwn(total.line())}"}]},
    ]


def build_batch_summary(outcomes: Sequence[PostOutcome], dry_run: bool) -> str:
    total = sum((o.usage for o in outcomes), Usage())
    rows = [f"| {o.category} | {'✅' if o.ok else '⚠️'} {o.status} | {o.url or ''} |" for o in outcomes]
    title = "카테고리별 테스트" if dry_run else "카테고리별 포스팅"
    return "\n".join([f"## 📚 {title} 결과", "", "| 카테고리 | 결과 | 링크 |", "|---|---|---|", *rows, "",
                      f"합계: {total.line()}"]) + "\n"


def run_categories(cfg: Config, options: RunOptions, services: Services,
                   categories: Sequence[str] = CATEGORIES) -> int:
    outcomes: list[PostOutcome] = []
    avoid: list[tuple[str, str]] = []
    for category in categories:
        opts = replace(options, category=category, topic="", avoid=tuple(avoid))
        before = services.usage() if services.usage else Usage()
        try:
            outcome = post_once(cfg, opts, services)
        except AlreadyReported as exc:  # 진행 스레드에 이미 보고됨 — 다음 카테고리로
            outcome = PostOutcome(1, category, status=f"오류: {str(exc)[:120]}")
        except Exception as exc:
            log.exception("%s 포스팅 중 오류", category)
            services.slack.notify_error(f"{category} 포스팅", exc)
            outcome = PostOutcome(1, category, status=f"오류: {str(exc)[:120]}")
        if outcome.usage == Usage() and services.usage:  # 오류로 끝난 글도 쓴 만큼은 합계에
            outcome = replace(outcome, usage=services.usage() - before)
        if outcome.topic:
            avoid.append((outcome.topic, outcome.source_url))
        outcomes.append(outcome)
        log.info("[%s] %s", category, outcome.status)
    services.slack.post(f"📚 {LABEL} 카테고리별 포스팅 결과", build_batch_blocks(outcomes, options.dry_run))
    write_step_summary(build_batch_summary(outcomes, options.dry_run))
    return 0 if any(o.ok for o in outcomes) else 1
