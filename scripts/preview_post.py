"""내 PC에서 미리보기 — 블로그·이미지 저장소·슬랙에 아무것도 올리지 않고 한 편을 끝까지 만들어 본다.

    python scripts/preview_post.py                     # 최신 AI 소식에서 주제 자동 선정
    python scripts/preview_post.py --topic "GPT-6"     # 주제 지정
    python scripts/preview_post.py --no-check          # 사실 검증 생략 (빠른 문법 확인용)

필요한 것: .env 의 GEMINI_API_KEY 하나 (구글 검색 근거, 무료 한도).
결과: output/preview_<시각>/ 에 sources.md(출처·사실·인용 이미지), quote_I*.jpg(인용 이미지), post.txt(원고),
      factcheck.md(사실 검증 보고), cover.png, blog_preview_google.html (구글 블로그에 올라갈 모습 — 업로드하지 않음)
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from autopost.cli import http_session  # noqa: E402
from autopost.config import load_config  # noqa: E402
from autopost.cover import make_cover  # noqa: E402
from autopost.fact_check import FactChecker, FactCheckReport  # noqa: E402
from autopost.gemini import Gemini  # noqa: E402
from autopost.history import PostHistory  # noqa: E402
from autopost.pipeline import KST, MIN_KEPT_RATIO  # noqa: E402
from autopost.prune import Pruner, removed_markdown  # noqa: E402
from autopost.publishing import PublishContext, attach_references, base_notes, write_preview  # noqa: E402
from autopost.quote_images import ImageCollector  # noqa: E402
from autopost.research import Researcher  # noqa: E402
from autopost.style_profile import load_style  # noqa: E402
from autopost.topic import TopicSelector  # noqa: E402
from autopost.writer import PostWriter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 포스팅 로컬 미리보기 (발행 안 함)")
    parser.add_argument("--topic", default="", help="쓸 주제 (비우면 자동 선정)")
    parser.add_argument("--no-check", action="store_true", help="사실 검증 생략")
    args = parser.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cfg = load_config()
    if not cfg.gemini_api_key:
        print("❌ .env 에 GEMINI_API_KEY 를 넣으세요 (https://aistudio.google.com/apikey)")
        return 1

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    workdir = cfg.output_dir / f"preview_{now:%Y%m%d_%H%M%S}"
    workdir.mkdir(parents=True, exist_ok=True)
    gemini = Gemini(cfg.gemini_api_key, cfg.gemini_model, cfg.gemini_fallback_models)
    session = http_session()

    topic, seen = TopicSelector(gemini, session, lookback_days=cfg.topic_lookback_days).pick(PostHistory(), now, args.topic)
    print(f"\n① 주제: {topic.title} ({topic.category}) — 후보 {seen}개\n   {topic.source_url}")
    sheet = Researcher(gemini, session, min_sources=cfg.min_references).research(topic, today)
    if sheet.image_candidates:
        sheet = replace(sheet, images=ImageCollector(session).collect(sheet.image_candidates, sheet.sources, workdir))
    (workdir / "sources.md").write_text(sheet.markdown(), encoding="utf-8")
    print(f"② 자료 조사: 사실 {len(sheet.facts)}개 · 출처 {len(sheet.sources)}개 · "
          f"인용 이미지 {len(sheet.images)}/{len(sheet.image_candidates)}개")

    writer = PostWriter(gemini, cfg.blog_name, cfg.min_body_chars)
    content = writer.write(sheet, load_style(topic.category, cfg.min_body_chars), today)
    (workdir / "draft_unverified.txt").write_text(content.plain_text() + "\n", encoding="utf-8")
    print(f"③ 원고: {content.title} — 본문 {content.text_length():,}자 (검증 전 원고: draft_unverified.txt)")

    report = FactCheckReport(())
    if not args.no_check:
        checker = FactChecker(gemini)
        report = checker.check(content, sheet, today)
        fixes = 0
        while not report.passed and fixes < cfg.max_fix_rounds:
            fixes += 1
            content = writer.revise(content, sheet, [v.as_issue() for v in report.issues])
            report = checker.check(content, sheet, today, rounds=fixes + 1)
        extra = ""
        if not report.passed:  # 실제 실행과 같게: 남은 틀림·확인 불가 주장이 든 문장·항목만 뺀다
            before = content.text_length()
            result = Pruner(gemini).prune(content, report.issues)
            content, extra = result.content, removed_markdown(result.removed, result.unlocated)
            kept = content.text_length() / max(before, 1)
            verdict = ("발행 보류 (위치 못 찾은 주장 남음)" if result.unlocated else
                       "발행 보류 (너무 많이 빠짐)" if kept < MIN_KEPT_RATIO or len(content.sections) < 2 else
                       f"문제 문장·항목 {len(result.removed)}곳 빼고 발행 가능 (본문 {kept:.0%} 유지)")
        else:
            verdict = "통과"
        (workdir / "factcheck.md").write_text(report.markdown(content.title) + extra, encoding="utf-8")
        print(f"④ 사실 검증: {report.summary_line()} → {verdict}")

    content = attach_references(content, sheet, now)
    (workdir / "post.txt").write_text(content.plain_text() + "\n", encoding="utf-8")
    cover = make_cover(workdir / "cover.png", content.title, content.category, cfg.blog_name, f"{now:%Y.%m.%d}")
    ctx = PublishContext(content, cover, base_notes(now, report.passed), PostHistory(), now.strftime("%Y%m%d_%H%M"),
                         sheet.images, topic.title, topic.source_url)
    preview = write_preview(ctx, workdir, dry_run=True)
    print(f"\n✅ 완료: {workdir}\n   {preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
