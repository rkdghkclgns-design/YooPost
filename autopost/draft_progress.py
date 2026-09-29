"""오늘 원고 진행 상황 — 카테고리 7개가 어디까지 됐는지 슬랙 #yoopost-알림 메시지 1개로 보여 주고 계속 갱신한다.

    📚 AI백과사전 오늘 원고 (09/29)   ▰▰▰▱▱▱▱ 3/7
    1. AI 언어 모델 — ✅ 발행됨: Holo4 총정리 …
    2. AI 도구 — 📝 승인 대기: Manus 2.0 사용 가이드 …
    3. AI 논문 분석 — ✍️ 작성 중
    4. AI 뉴스 리뷰 — ⬜ 대기

원고 폴더(output/claude_drafts/<날짜시각>_<카테고리 번호>)와 원고 보관함의 상태(pending·approved·published)만 읽는다 (AI 호출 없음).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from .constants import CATEGORIES
from .slack_helper import LABEL, escape_mrkdwn

log = logging.getLogger(__name__)

RUNNING_MINUTES = 40  # 이 시간 안에 만들어진, 아직 보내지 않은 폴더는 '작성 중'
BAR_ON, BAR_OFF = "▰", "▱"


@dataclass(frozen=True)
class Draft:
    folder: Path
    category: str
    title: str = ""
    sent: bool = False  # 슬랙 승인 요청까지 보냄 (publish.html 이 있음)
    started: float = 0.0


@dataclass(frozen=True)
class Row:
    category: str
    icon: str
    text: str
    finished: bool  # 승인 요청을 보냈거나 건너뜀


def scan(drafts_dir: Path, day: str) -> list[Draft]:
    """오늘(YYYYMMDD) 원고 폴더 — 폴더 이름 끝 번호가 카테고리 순서."""
    found: list[Draft] = []
    if not drafts_dir.is_dir():
        return found
    for folder in sorted(drafts_dir.glob(f"{day}_*")):
        suffix = folder.name.rsplit("_", 1)[-1]
        if not folder.is_dir() or not suffix.isdigit() or not 1 <= int(suffix) <= len(CATEGORIES):
            continue
        title = ""
        post = folder / "post.json"
        if post.is_file():
            try:
                title = str(json.loads(post.read_text(encoding="utf-8-sig")).get("title") or "")
            except (OSError, json.JSONDecodeError):
                title = ""
        found.append(Draft(folder, CATEGORIES[int(suffix) - 1], title, (folder / "publish.html").is_file(),
                           folder.stat().st_mtime))
    return found


def _state(store, draft: Draft) -> tuple[str, str]:
    """(아이콘, 설명) — 원고 보관함에서 발행 여부를 읽는다 (못 읽으면 승인 대기로)."""
    status, url = "pending", ""
    if store is not None:
        try:
            data = store.get(draft.folder.name)
            status, url = str(data.get("status") or "pending"), str(data.get("url") or "")
        except Exception as exc:
            log.info("원고 상태 확인 실패 %s: %s", draft.folder.name, exc)
    title = escape_mrkdwn(draft.title[:60])
    if status == "published":
        return "✅", f"발행됨: <{url}|{title}>" if url else f"발행됨: {title}"
    if status == "approved":
        return "🚀", f"발행 중: {title}"
    if status == "failed":
        return "❌", f"발행 실패 (✅ 다시 누르면 재시도): {title}"
    return "📝", f"승인 대기: {title}"


def build_rows(drafts: Sequence[Draft], skipped: dict[str, str], store, now: float) -> list[Row]:
    rows: list[Row] = []
    for category in CATEGORIES:
        mine = [d for d in drafts if d.category == category]
        sent = [d for d in mine if d.sent]
        if sent:
            parts = [_state(store, d) for d in sent]
            icon = parts[0][0]
            text = " · ".join(p[1] for p in parts)
            rows.append(Row(category, icon, text, True))
        elif category in skipped:
            rows.append(Row(category, "⏭️", f"건너뜀 — {escape_mrkdwn(skipped[category])}", True))
        elif any(now - d.started < RUNNING_MINUTES * 60 for d in mine):
            rows.append(Row(category, "✍️", "작성 중", False))
        else:
            rows.append(Row(category, "⬜", "대기", False))
    return rows


def progress_bar(done: int, total: int) -> str:
    return BAR_ON * done + BAR_OFF * (total - done) + f" {done}/{total}"


def build_blocks(rows: Sequence[Row], day_label: str) -> tuple[str, list[dict]]:
    done = sum(1 for r in rows if r.finished)
    head = f"📚 {LABEL} 오늘 원고 ({day_label})  {progress_bar(done, len(rows))}"
    lines = "\n".join(f"{i}. {r.icon} *{escape_mrkdwn(r.category)}* — {r.text}" for i, r in enumerate(rows, start=1))
    waiting = sum(1 for r in rows if r.icon == "📝")
    footer = (f"승인 대기 {waiting}편 — #yoopost-1-원고승인 의 원고 메시지에 ✅ 를 누르면 발행됩니다"
              if waiting else "원고는 #yoopost-1-원고승인 에 올라갑니다")
    return head, [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{head}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": lines[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]},
    ]


class ProgressBoard:
    """오늘 진행 메시지 하나를 올리고(처음) 이후에는 같은 메시지를 고친다."""

    def __init__(self, slack, drafts_dir: Path, state_file: Path, store=None,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self._slack = slack
        self._drafts = drafts_dir
        self._state_file = state_file
        self._store = store
        self._now = now

    def _load(self) -> dict:
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def skip(self, category: str, reason: str) -> None:
        data = self._load()
        self._save({**data, "skipped": {**data.get("skipped", {}), category: reason}})

    def refresh(self) -> list[Row]:
        now = self._now()
        data = self._load()
        rows = build_rows(scan(self._drafts, now.strftime("%Y%m%d")), data.get("skipped", {}), self._store,
                          now.timestamp())
        text, blocks = build_blocks(rows, now.strftime("%m/%d"))
        ts = data.get("ts")
        if not (ts and self._slack.update(ts, text, blocks)):
            ts = self._slack.post(text, blocks)
        self._save({**data, "ts": ts})
        return rows
