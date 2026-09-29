"""포스팅/초기세팅 전체 흐름.

    ① 셀프 세팅 점검 → ② 주제 선정 → ③ 자료 조사·출처 확인 → ④ AI 원고 → ⑤ 사실 검증(틀리면 고쳐서 재검증)
    → ⑥ 대표 이미지·미리보기 → ⑦ 구글 블로그 발행 → ⑧ 발행 기록
사실 검증을 끝내 통과하지 못하면 발행하지 않고 원고·검증 보고만 슬랙에 남긴다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Callable

from .blog_publisher import BloggerPublisher
from .config import Config, platform_readiness
from .content import PostContent
from .cover import make_cover
from .drive_helper import DriveHelper, folder_url
from .fact_check import FactChecker, FactCheckReport
from .history import HistoryEntry, HistoryStore, PostHistory
from .progress import ProgressReporter
from .publishing import (
    PublishContext,
    attach_references,
    base_notes,
    publish_blogger,
    published_urls,
    skipped_results,
    write_preview,
)
from .research import FactSheet, Researcher
from .setup_engine import SetupReport, ensure_layout
from .slack_helper import SlackNotifier, build_report_blocks, build_setup_blocks, escape_mrkdwn
from .step_summary import build_failure_summary, build_step_summary, write_step_summary
from .style_profile import load_style
from .topic import Topic, TopicSelector
from .writer import PostWriter

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
TRIGGER_LABELS = {
    "schedule": "정기 자동 실행",
    "workflow_dispatch": "수동 실행",
    "slack": "슬랙 명령",
    "manual": "로컬 실행",
}


@dataclass(frozen=True)
class RunOptions:
    topic: str = ""  # 직접 지정한 주제 (비우면 자동 선정)
    dry_run: bool = False
    trigger: str = "manual"
    requested_by: str = ""
    run_url: str = ""  # GitHub Actions 실행 로그 주소 (슬랙 진행 메시지에 링크)


@dataclass(frozen=True)
class Services:
    drive: DriveHelper
    slack: SlackNotifier
    selector: TopicSelector | None = None
    researcher: Researcher | None = None
    writer: PostWriter | None = None
    checker: FactChecker | None = None
    blogger: BloggerPublisher | None = None
    cover_maker: Callable[..., Path] = make_cover
    now: Callable[[], datetime] = lambda: datetime.now(KST)


class AlreadyReported(RuntimeError):
    """진행 상황 스레드에 이미 오류를 보고함 — 상위에서 중복 알림을 보내지 않는다."""


def short(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- 초기 세팅
def _setup(cfg: Config, services: Services) -> SetupReport:
    return ensure_layout(services.drive, cfg.drive_root_name)


def run_setup(cfg: Config, services: Services) -> int:
    report = _setup(cfg, services)
    blocks = build_setup_blocks(report.created_folders, report.created_files, platform_readiness(cfg),
                                folder_url(report.layout.root_id))
    services.slack.post("✅ [AI백과사전] 초기 세팅 완료", blocks)
    return 0


# ---------------------------------------------------------------- 포스팅
def _meta(options: RunOptions, now: datetime) -> list[str]:
    # 트리거·요청자는 Actions 수동 입력(자유 텍스트)에서 올 수 있어 멘션·링크가 되지 않도록 이스케이프
    meta = [escape_mrkdwn(TRIGGER_LABELS.get(options.trigger, options.trigger))]
    if options.topic:
        meta.append(f"요청 주제 *{escape_mrkdwn(short(options.topic, 60))}*")
    if options.requested_by:
        meta.append(f"요청 {escape_mrkdwn(options.requested_by)}")
    meta.append(f"{now:%m/%d %H:%M}")
    return meta


def run_post(cfg: Config, options: RunOptions, services: Services) -> int:
    if None in (services.selector, services.researcher, services.writer, services.checker):
        raise RuntimeError("포스팅 모드에는 주제 선정·조사·작성·검증 서비스가 모두 필요합니다")
    report = _setup(cfg, services)  # 1. 셀프 세팅 점검
    if report.changed:
        services.slack.post(f"🛠️ 셀프 세팅: 누락된 항목 {len(report.created_folders) + len(report.created_files)}개를 자동 생성했습니다")

    now = services.now()
    tag = " [테스트]" if options.dry_run else ""
    workdir = cfg.output_dir / f"{now:%Y%m%d_%H%M}_ai"
    store = HistoryStore(services.drive, report.layout.root_id, workdir)
    reporter = ProgressReporter(
        services.slack, f"🤖 AI 포스팅 진행 중{tag}", _meta(options, now), options.run_url,
        stage_channels=dict(cfg.slack_stage_channels),
        run_label=f"{now:%m/%d %H:%M}" + (" · 테스트" if options.dry_run else ""),
    )
    reporter.start()
    try:
        return _run_steps(cfg, options, services, report, store, reporter, now, workdir, tag)
    except Exception as exc:
        failed_step = reporter.fail_current(exc)
        write_step_summary(build_failure_summary("AI 포스팅", failed_step, exc))
        raise AlreadyReported(str(exc)) from exc


def _pick_topic(services: Services, options: RunOptions, history: PostHistory, now: datetime,
                reporter: ProgressReporter, tag: str) -> Topic:
    reporter.step("topic", "run")
    topic, seen = services.selector.pick(history, now, options.topic)
    # 주제 제목은 RSS·AI 응답(외부 입력)이라 슬랙 멘션·링크가 되지 않도록 이스케이프
    title = escape_mrkdwn(topic.title)
    run_label = f"{short(title, 28)} · {now:%m/%d %H:%M}" + (" · 테스트" if options.dry_run else "")
    reporter.retitle(f"🤖 AI 포스팅 진행 중{tag} — {short(title, 60)}", run_label)
    origin = "요청 주제" if topic.requested else (f"후보 {seen}개 중 선택" if seen else "웹 검색으로 선택")
    source = f"{'공식' if topic.official else '뉴스'}·{topic.source_name}" if topic.source_name else "출처 조사 중"
    reporter.step("topic", "done", f"{origin} ({source}) · {topic.category}")
    return topic


def _research(services: Services, topic: Topic, today: str, workdir: Path, reporter: ProgressReporter) -> FactSheet:
    reporter.step("research", "run")
    sheet = services.researcher.research(topic, today)
    official = sum(1 for s in sheet.sources if s.official)
    reporter.step("research", "done", f"사실 {len(sheet.facts)}개 · 출처 {len(sheet.sources)}개 (공식 {official})")
    path = workdir / "sources.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sheet.markdown(), encoding="utf-8")
    reporter.file(path, "출처·사실 목록",
                  f"🔎 *{escape_mrkdwn(topic.title)}*\n원문: {escape_mrkdwn(topic.source_url) or '웹 검색'}",
                  stage="research")
    return sheet


def _verify(services: Services, cfg: Config, content: PostContent, sheet: FactSheet, today: str,
            reporter: ProgressReporter) -> tuple[PostContent, FactCheckReport]:
    """검증 → 문제가 있으면 원고 수정 → 재검증 (최대 MAX_FIX_ROUNDS 회)."""
    reporter.step("factcheck", "run")
    report = services.checker.check(content, sheet, today, rounds=1)
    fixes = 0
    while not report.passed and fixes < cfg.max_fix_rounds:
        fixes += 1
        reporter.step("factcheck", "run", f"{report.summary_line()} → 원고 수정 {fixes}회차")
        content = services.writer.revise(content, sheet, [v.as_issue() for v in report.issues])
        report = services.checker.check(content, sheet, today, rounds=fixes + 1)
    reporter.step("factcheck", "done" if report.passed else "fail", report.summary_line())
    return content, report


def _keep_unverified_draft(content: PostContent, workdir: Path, reporter: ProgressReporter) -> None:
    """사실 검증을 끝내지 못해도(AI 과부하 등) 써 둔 원고는 남긴다 — 발행은 하지 않는다."""
    path = workdir / "draft_unverified.txt"
    path.write_text(content.plain_text() + "\n", encoding="utf-8")
    reporter.file(path, "사실 검증 전 원고",
                  f"⚠️ 사실 검증을 끝내지 못한 원고 — 발행하지 않았습니다: *{escape_mrkdwn(content.title)}*", stage="draft")


def _write_outputs(content: PostContent, report: FactCheckReport, workdir: Path, reporter: ProgressReporter) -> None:
    post_txt = workdir / "post.txt"
    post_txt.write_text(content.plain_text() + "\n", encoding="utf-8")
    fact_md = workdir / "factcheck.md"
    fact_md.write_text(report.markdown(content.title), encoding="utf-8")
    reporter.file(post_txt, "원고", f"📝 *{escape_mrkdwn(content.title)}*\n본문 {content.text_length():,}자 · "
                  f"해시태그: {escape_mrkdwn(' '.join(content.hashtags[:6]))} …", stage="draft")
    icon = "✅" if report.passed else "⚠️"
    reporter.file(fact_md, "사실 검증 보고", f"{icon} 사실 검증 — {report.summary_line()}", stage="draft")


def _run_steps(cfg: Config, options: RunOptions, services: Services, setup: SetupReport, store: HistoryStore,
               reporter: ProgressReporter, now: datetime, workdir: Path, tag: str) -> int:
    today = now.strftime("%Y-%m-%d")
    history = store.load()
    topic = _pick_topic(services, options, history, now, reporter, tag)
    sheet = _research(services, topic, today, workdir, reporter)

    reporter.step("draft", "run")
    content = services.writer.write(sheet, load_style(topic.category, cfg.min_body_chars), today)
    reporter.step("draft", "done", f"본문 {content.text_length():,}자 · 소제목 {len(content.sections)}개")

    try:
        content, report = _verify(services, cfg, content, sheet, today, reporter)
    except Exception:
        _keep_unverified_draft(content, workdir, reporter)
        raise
    content = attach_references(content, sheet, now)
    _write_outputs(content, report, workdir, reporter)

    reporter.step("image", "run")
    cover = services.cover_maker(workdir / "cover.png", content.title, content.category, cfg.blog_name, f"{now:%Y.%m.%d}")
    reporter.step("image", "done", "1200×630")
    ctx = PublishContext(content, cover, base_notes(now, report.passed), history, setup.layout.public_folder_id,
                         now.strftime("%Y%m%d_%H%M"))
    preview = write_preview(ctx, workdir, options.dry_run)
    reporter.file(cover, "대표 이미지", "🖼️ 대표 이미지", stage="draft")
    reporter.file(preview, "구글 블로그 미리보기", "👀 구글 블로그에 올라갈 모습 — 내려받아 브라우저로 열어 보세요", stage="draft")

    held = not report.passed
    if options.dry_run:
        results = skipped_results("테스트 실행(dry-run) — 발행 안 함")
        reporter.step("publish", "skip", "테스트 실행 — 발행 안 함")
    elif held:
        results = skipped_results("사실 검증 미통과 — 발행 보류")
        reporter.step("publish", "skip", "사실 검증 미통과 — 발행 보류")
    else:
        reporter.step("publish", "run")
        results = [publish_blogger(services.drive, services.blogger, ctx)]
        reporter.step("publish", "done" if results[0].ok else "fail", results[0].url or results[0].message)

    _record(store, history, topic, content, results, options, reporter, today)
    reporter.note(f"🤖 [AI백과사전] {escape_mrkdwn(content.title)}",
                  build_report_blocks(content.title, content.category, results, report.summary_line(),
                                      options.dry_run, held and not options.dry_run), stage="publish")
    ok = options.dry_run or (not held and any(r.ok for r in results))
    headline = ("🧪 테스트 완료" if options.dry_run else "⚠️ 사실 검증 미통과 — 발행 보류" if held
                else "✅ 포스팅 완료" if ok else "❌ 포스팅 실패")
    reporter.finish(f"{headline} — {short(escape_mrkdwn(content.title), 60)}")
    write_step_summary(build_step_summary(content.title, content.category, topic.source_url, content.text_length(),
                                          report.summary_line(), results, options.dry_run, held))
    return 0 if ok else 1


def _record(store: HistoryStore, history: PostHistory, topic: Topic, content: PostContent, results, options: RunOptions,
            reporter: ProgressReporter, today: str) -> None:
    """발행에 성공했을 때만 기록 (실패·테스트는 기록하지 않아 다음에 다시 쓸 수 있음)."""
    if options.dry_run or not any(r.ok for r in results):
        reporter.step("record", "skip", "발행하지 않아 기록 안 함")
        return
    reporter.step("record", "run")
    entry = HistoryEntry(today, content.title, topic.title, content.category, topic.source_url,
                         MappingProxyType(published_urls(results)))
    store.save(history.add(entry))
    reporter.step("record", "done", f"발행 기록 {len(history.entries) + 1}번째")
