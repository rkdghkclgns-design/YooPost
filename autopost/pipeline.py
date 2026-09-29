"""포스팅/연동 점검 전체 흐름 (구글 드라이브는 쓰지 않는다).

    ① 주제 선정 → ② 자료 조사(구글 검색)·출처 확인·인용 이미지 확보 → ③ AI 원고
    → ④ 사실 검증(틀리면 고쳐서 재검증) → ⑤ 대표 이미지·미리보기 → ⑥ 구글 블로그 발행
발행 기록은 구글 블로그 글 목록 자체다 (같은 주제 반복 방지·함께 보면 좋은 글).
원고를 고쳐도 사실 검증을 통과하지 못하면, 틀림·확인 불가 주장이 든 문장·항목만 빼고 발행한다 (글 전체를 멈추지 않음).
빼고 나면 글이 너무 짧아지거나 위치를 찾지 못한 주장이 남을 때만 발행을 보류한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .blog_publisher import BloggerPublisher
from .config import Config
from .content import PostContent
from .cover import make_cover
from .fact_check import FactChecker, FactCheckReport
from .history import PostHistory, load_history
from .image_host import ImageHost
from .progress import ProgressReporter
from .prune import Pruner, removed_markdown
from .publishing import (
    PublishContext,
    attach_references,
    base_notes,
    publish_blogger,
    skipped_results,
    write_preview,
)
from .quote_images import ImageCollector
from .research import FactSheet, Researcher
from .slack_helper import SlackNotifier, build_report_blocks, build_setup_blocks, escape_mrkdwn
from .step_summary import build_failure_summary, build_step_summary, write_step_summary
from .style_profile import load_style
from .topic import Topic, TopicSelector
from .writer import PostWriter

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
MIN_KEPT_RATIO = 0.6  # 문제 항목을 빼고 남은 본문이 원래의 60% 미만이면 글 자체를 믿기 어려워 보류
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
    slack: SlackNotifier
    selector: TopicSelector | None = None
    researcher: Researcher | None = None
    writer: PostWriter | None = None
    checker: FactChecker | None = None
    blogger: BloggerPublisher | None = None
    images: ImageCollector | None = None  # 인용 이미지 내려받기·확인
    image_host: ImageHost | None = None  # 대표·인용 이미지 공개 주소 (Cloudflare Worker)
    pruner: Pruner | None = None  # 검증 미통과 주장만 빼기 (없으면 글 전체 발행 보류)
    cover_maker: Callable[..., Path] = make_cover
    now: Callable[[], datetime] = lambda: datetime.now(KST)


@dataclass(frozen=True)
class FactOutcome:
    """사실 검증 최종 결과 — 통과 / 문제 항목만 빼고 발행 / 발행 보류."""
    report: FactCheckReport
    removed: tuple[str, ...] = ()  # 발행 전에 뺀 문장·항목
    unlocated: tuple[str, ...] = ()  # 원고에서 위치를 찾지 못한 주장
    held_reason: str = ""

    @property
    def publishable(self) -> bool:
        return not self.held_reason and (self.report.passed or bool(self.removed) or not self.report.issues)

    def line(self) -> str:
        text = self.report.summary_line()
        if self.removed:
            text += f" → 문제 문장·항목 {len(self.removed)}곳 빼고 발행"
        if self.held_reason:
            text += f" → {self.held_reason}"
        return text


class AlreadyReported(RuntimeError):
    """진행 상황 스레드에 이미 오류를 보고함 — 상위에서 중복 알림을 보내지 않는다."""


def short(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- 연동 점검
def setup_checks(cfg: Config, services: Services) -> tuple[list[tuple[str, bool, str]], str]:
    """(항목, 정상 여부, 안내) 목록과 블로그 주소 — 실제로 연결해 본다."""
    checks: list[tuple[str, bool, str]] = []
    blog_url = ""
    try:
        info = services.blogger.blog_info() if services.blogger else None
    except Exception as exc:
        info = None
        blog_hint = f"연결 실패: {str(exc)[:80]}"
    else:
        blog_hint = "BLOGGER_BLOG_ID 와 GOOGLE_* 시크릿" if info is None else ""
    if info:
        blog_url = info["url"]
        checks.append((f"구글 블로그 — {info['name']} (글 {info['posts']}개)", True, ""))
    else:
        checks.append(("구글 블로그", False, blog_hint))
    checks.append(("Gemini 자료 조사·원고·사실 검증 (구글 검색)", bool(cfg.gemini_api_key), "GEMINI_API_KEY"))
    host_ok = services.image_host.ping() if services.image_host else False
    checks.append(("이미지 저장소 (대표·인용 이미지)", host_ok,
                   "IMAGE_HOST_URL + IMAGE_UPLOAD_TOKEN (scripts/image_host_secret.py)"))
    checks.append(("슬랙", cfg.slack_enabled, "SLACK_BOT_TOKEN + SLACK_CHANNEL_ID 또는 SLACK_WEBHOOK_URL"))
    return checks, blog_url


def run_setup(cfg: Config, services: Services) -> int:
    checks, blog_url = setup_checks(cfg, services)
    ok = all(passed for _, passed, _ in checks)
    services.slack.post("✅ [AI백과사전] 연동 점검 완료" if ok else "⚠️ [AI백과사전] 연동 점검 — 확인 필요",
                        build_setup_blocks(checks, blog_url))
    return 0 if ok else 1


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
    now = services.now()
    tag = " [테스트]" if options.dry_run else ""
    workdir = cfg.output_dir / f"{now:%Y%m%d_%H%M}_ai"
    reporter = ProgressReporter(
        services.slack, f"🤖 AI 포스팅 진행 중{tag}", _meta(options, now), options.run_url,
        stage_channels=dict(cfg.slack_stage_channels),
        run_label=f"{now:%m/%d %H:%M}" + (" · 테스트" if options.dry_run else ""),
    )
    reporter.start()
    try:
        return _run_steps(cfg, options, services, reporter, now, workdir, tag)
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
    if services.images is not None and sheet.image_candidates:
        sheet = replace(sheet, images=services.images.collect(sheet.image_candidates, sheet.sources, workdir))
    official = sum(1 for s in sheet.sources if s.official)
    images = f" · 인용 이미지 {len(sheet.images)}개" if sheet.image_candidates else " · 인용 이미지 불필요"
    reporter.step("research", "done", f"사실 {len(sheet.facts)}개 · 출처 {len(sheet.sources)}개 (공식 {official}){images}")
    path = workdir / "sources.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sheet.markdown(), encoding="utf-8")
    reporter.file(path, "출처·사실 목록",
                  f"🔎 *{escape_mrkdwn(topic.title)}*\n원문: {escape_mrkdwn(topic.source_url) or '웹 검색'}",
                  stage="research")
    for image in sheet.images:
        reporter.file(image.path, f"인용 이미지 {image.id}",
                      f"🖼️ {escape_mrkdwn(image.caption)} (출처: {escape_mrkdwn(image.credit)})", stage="research")
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
    return content, report


def _exclude_issues(services: Services, cfg: Config, content: PostContent, sheet: FactSheet, today: str,
                    report: FactCheckReport, reporter: ProgressReporter) -> tuple[PostContent, FactOutcome]:
    """고쳐도 남은 틀림·확인 불가 주장이 든 문장·항목만 뺀다 — 위치를 못 찾은 주장이 있으면 한 번 더 검증."""
    if services.pruner is None:
        return content, FactOutcome(report, held_reason="사실 검증 미통과 — 발행 보류")
    reporter.step("factcheck", "run", f"{report.summary_line()} → 틀림·확인 불가 항목 빼는 중")
    result = services.pruner.prune(content, report.issues)
    pruned, removed, unlocated = result.content, result.removed, result.unlocated
    if unlocated:
        report = services.checker.check(pruned, sheet, today, rounds=report.rounds + 1)
        unlocated = ()
        if not report.passed:
            again = services.pruner.prune(pruned, report.issues)
            pruned, removed, unlocated = again.content, removed + again.removed, again.unlocated
    held = ""
    if unlocated:
        held = f"위치를 찾지 못한 주장 {len(unlocated)}개가 남아 발행 보류"
    elif len(pruned.sections) < 2 or pruned.text_length() < content.text_length() * MIN_KEPT_RATIO:
        held = "문제 항목을 빼면 본문이 너무 짧아져 발행 보류"
    return pruned, FactOutcome(report, removed, unlocated, held)


def _keep_unverified_draft(content: PostContent, workdir: Path, reporter: ProgressReporter) -> None:
    """사실 검증을 끝내지 못해도(AI 과부하 등) 써 둔 원고는 남긴다 — 발행은 하지 않는다."""
    path = workdir / "draft_unverified.txt"
    path.write_text(content.plain_text() + "\n", encoding="utf-8")
    reporter.file(path, "사실 검증 전 원고",
                  f"⚠️ 사실 검증을 끝내지 못한 원고 — 발행하지 않았습니다: *{escape_mrkdwn(content.title)}*", stage="draft")


def _write_outputs(content: PostContent, outcome: FactOutcome, workdir: Path, reporter: ProgressReporter) -> None:
    post_txt = workdir / "post.txt"
    post_txt.write_text(content.plain_text() + "\n", encoding="utf-8")
    fact_md = workdir / "factcheck.md"
    extra = removed_markdown(outcome.removed, outcome.unlocated) if outcome.removed or outcome.unlocated else ""
    fact_md.write_text(outcome.report.markdown(content.title) + extra, encoding="utf-8")
    reporter.file(post_txt, "원고", f"📝 *{escape_mrkdwn(content.title)}*\n본문 {content.text_length():,}자 · "
                  f"해시태그: {escape_mrkdwn(' '.join(content.hashtags[:6]))} …", stage="draft")
    icon = "✅" if outcome.report.passed else "✂️" if outcome.publishable else "⚠️"
    reporter.file(fact_md, "사실 검증 보고", f"{icon} 사실 검증 — {outcome.line()}", stage="draft")


def _run_steps(cfg: Config, options: RunOptions, services: Services, reporter: ProgressReporter, now: datetime,
               workdir: Path, tag: str) -> int:
    today = now.strftime("%Y-%m-%d")
    history = load_history(services.blogger)
    topic = _pick_topic(services, options, history, now, reporter, tag)
    sheet = _research(services, topic, today, workdir, reporter)

    reporter.step("draft", "run")
    content = services.writer.write(sheet, load_style(topic.category, cfg.min_body_chars), today)
    reporter.step("draft", "done", f"본문 {content.text_length():,}자 · 소제목 {len(content.sections)}개")

    try:
        content, report = _verify(services, cfg, content, sheet, today, reporter)
        outcome = FactOutcome(report)
        if not report.passed:
            content, outcome = _exclude_issues(services, cfg, content, sheet, today, report, reporter)
    except Exception:
        _keep_unverified_draft(content, workdir, reporter)
        raise
    reporter.step("factcheck", "done" if outcome.publishable else "fail", outcome.line())
    content = attach_references(content, sheet, now)
    _write_outputs(content, outcome, workdir, reporter)

    reporter.step("image", "run")
    cover = services.cover_maker(workdir / "cover.png", content.title, content.category, cfg.blog_name, f"{now:%Y.%m.%d}")
    ctx = PublishContext(content, cover, base_notes(now, outcome.publishable), history, now.strftime("%Y%m%d_%H%M"),
                         sheet.images, topic.title, topic.source_url)
    used = len(ctx.used_images())
    reporter.step("image", "done", "대표 1200×630" + (f" · 인용 {used}개" if used else ""))
    preview = write_preview(ctx, workdir, options.dry_run)
    reporter.file(cover, "대표 이미지", "🖼️ 대표 이미지", stage="draft")
    reporter.file(preview, "구글 블로그 미리보기", "👀 구글 블로그에 올라갈 모습 — 내려받아 브라우저로 열어 보세요", stage="draft")

    held = not outcome.publishable
    if options.dry_run:
        results = skipped_results("테스트 실행(dry-run) — 발행 안 함")
        reporter.step("publish", "skip", "테스트 실행 — 발행 안 함")
    elif held:
        results = skipped_results(outcome.held_reason)
        reporter.step("publish", "skip", outcome.held_reason)
    else:
        reporter.step("publish", "run")
        results = [publish_blogger(services.image_host, services.blogger, ctx)]
        reporter.step("publish", "done" if results[0].ok else "fail", results[0].url or results[0].message)

    reporter.note(f"🤖 [AI백과사전] {escape_mrkdwn(content.title)}",
                  build_report_blocks(content.title, content.category, results, outcome.line(),
                                      options.dry_run, held and not options.dry_run), stage="publish")
    ok = options.dry_run or (not held and any(r.ok for r in results))
    headline = ("🧪 테스트 완료" if options.dry_run else "⚠️ 사실 검증 미통과 — 발행 보류" if held
                else "✅ 포스팅 완료" if ok else "❌ 포스팅 실패")
    reporter.finish(f"{headline} — {short(escape_mrkdwn(content.title), 60)}")
    write_step_summary(build_step_summary(content.title, content.category, topic.source_url, content.text_length(),
                                          outcome.line(), results, options.dry_run, held))
    return 0 if ok else 1
