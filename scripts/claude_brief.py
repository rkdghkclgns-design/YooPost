"""Claude 가 원고를 쓰기 전에 보는 작성 지침 — 참고 블로그 문법·사실 규칙·원고 형식·이미 쓴 주제를 한 번에 출력한다.

    python scripts/claude_brief.py                      # 카테고리 지정 없이
    python scripts/claude_brief.py --category "AI 보안"
    python scripts/claude_brief.py --new                # 새 원고 폴더를 만들고 경로 출력

원고 폴더(output/claude_drafts/<ID>/)에 post.json·research.json 을 쓴 뒤
    python scripts/claude_draft.py output/claude_drafts/<ID>
로 슬랙 승인 요청을 보낸다. (구글 AI API 를 쓰지 않는다)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from autopost.config import load_config  # noqa: E402
from autopost.constants import CATEGORIES, CATEGORY_HINTS, DEFAULT_CATEGORY  # noqa: E402
from autopost.pipeline import KST  # noqa: E402
from autopost.style_profile import load_style  # noqa: E402
from autopost.writer import FACT_RULES, OUTPUT_SCHEMA, style_rules  # noqa: E402

DRAFTS = ROOT / "output" / "claude_drafts"

RESEARCH_SCHEMA = """{
  "topic": "원 발표·기사 제목 (발행 기록·중복 방지에 씀)",
  "source_url": "대표 원문 주소 (공식 발표 우선)",
  "sources": [{"id": "S1", "title": "문서 제목", "url": "실제로 읽은 원래 주소", "publisher": "발행 주체",
               "published": "YYYY-MM-DD 또는 빈 문자열", "official": true}],
  "images": [{"id": "I1", "image_url": "이미지 파일 주소 (모르면 빈 문자열)", "page_url": "이미지가 실린 출처 페이지",
              "caption": "이미지 설명 한 줄", "credit": "저작권자"}],
  "factcheck": {"claims": 40, "confirmed": 40, "removed": ["확인되지 않아 뺀 문장"], "note": "검증 방법 메모"}
}"""

CLAUDE_STEPS = """[Claude 작업 순서 — 구글 AI API 없이]
1. 주제: 아래 '이미 쓴 글'과 겹치지 않는 최근 AI 소식을 웹 검색으로 고른다 (공식 발표·1차 자료 우선).
2. 조사: 공식 발표·문서·논문 원문을 직접 열어 읽고, 신뢰할 수 있는 언론으로 교차 확인한다. 사실마다 출처 번호.
3. 원고: 아래 글 문법·사실 규칙대로 post.json 을 쓴다 (형광 강조 **…**, 필요하면 image 블록).
4. 사실 검증: 원고의 수치·날짜·이름·가격·스펙 주장을 모두 뽑아, 원고와 별개로 출처를 다시 열어 확인한다.
   틀린 것은 고치고, 확인되지 않는 문장은 뺀다. 결과를 research.json 의 factcheck 에 적는다.
5. 원고 폴더에 post.json·research.json 을 저장하고 scripts/claude_draft.py 로 슬랙 승인 요청을 보낸다.
6. 슬랙 원고 메시지에 사용자가 ✅ 를 누르면 자동으로 발행된다 (Claude 가 ✅ 를 누르지 않는다)."""


def recent_titles(cfg, limit: int = 40) -> list[str]:
    try:
        from autopost.blog_publisher import BloggerPublisher
        from autopost.google_auth import build_blogger_service, build_credentials
        from autopost.history import load_history

        blogger = BloggerPublisher(build_blogger_service(build_credentials(cfg.google)), cfg.blogger_blog_id)
        history = load_history(blogger)
        return [f"{e.title}  (주제: {e.topic} · {e.source_url or '원문 없음'})" for e in history.entries[-limit:]][::-1]
    except Exception as exc:  # 목록을 못 읽어도 지침은 보여 준다
        return [f"(발행 기록을 읽지 못했습니다: {exc})"]


def new_draft_dir(category: str) -> Path:
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    number = CATEGORIES.index(category) + 1 if category in CATEGORIES else 0
    folder = DRAFTS / f"{stamp}_{number}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude 원고 작성 지침")
    parser.add_argument("--category", default="", choices=["", *CATEGORIES])
    parser.add_argument("--new", action="store_true", help="새 원고 폴더 만들기")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    cfg = load_config()
    category = args.category or DEFAULT_CATEGORY
    style = load_style(category, cfg.min_body_chars)
    today = datetime.now(KST).strftime("%Y-%m-%d")
    print(f"# AI백과사전 원고 작성 지침 — 오늘 {today}")
    if args.category:
        print(f"\n[카테고리] {args.category}: {CATEGORY_HINTS[args.category]} (post.json 의 category 는 반드시 이 값)")
    print("\n[카테고리 목록]\n" + "\n".join(f"- {c}: {CATEGORY_HINTS[c]}" for c in CATEGORIES))
    print("\n" + CLAUDE_STEPS)
    print("\n" + style_rules(style, cfg.blog_name))
    print("\n" + FACT_RULES)
    print(f"\n[분량] 본문(공백·코드블록 제외) {cfg.min_body_chars:,}~{style.target_chars:,}자")
    print("\n[post.json 형식]\n" + OUTPUT_SCHEMA)
    print("\n[research.json 형식]\n" + RESEARCH_SCHEMA)
    print("\n[이미 쓴 글 — 같은 주제·같은 원문 금지]")
    print("\n".join(f"- {t}" for t in recent_titles(cfg)) or "- (없음)")
    if args.new:
        print(f"\n[새 원고 폴더] {new_draft_dir(args.category)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
