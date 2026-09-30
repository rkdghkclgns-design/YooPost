"""오늘 원고 진행 상황 — 카테고리 7개가 어디까지 됐는지 슬랙 #yoopost-알림 메시지 1개로 보여 주고 계속 갱신한다.

    📚 AI백과사전 오늘 원고 (09/29)   ▰▰▰▱▱▱▱ 3/7
    1. AI 언어 모델 — ✅ 발행됨: Holo4 총정리 …
    2. AI 도구 — 📝 승인 대기: Manus 2.0 사용 가이드 …
    3. AI 논문 분석 — ✍️ 작성 중
    4. AI 뉴스 리뷰 — ⬜ 대기

원고 폴더(output/claude_drafts/<날짜시각>_<카테고리 번호>)와 원고 보관함의 상태(pending·approved·published)만 읽는다 (AI 호출 없음).
클라우드 루틴의 원고(yoopost-drafts 브랜치)도 같은 방식으로 보여 준다 — 보관함에 있으면 보낸 원고, skip.json 은 건너뜀.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .constants import CATEGORIES
from .draft_request import request_id_of
from .slack_helper import LABEL, escape_mrkdwn

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))  # 원고 폴더 이름의 시각 기준
RUNNING_MINUTES = 40  # 이 시간 안에 만들어진, 아직 보내지 않은 폴더는 '작성 중'
BAR_ON, BAR_OFF = "▰", "▱"


@dataclass(frozen=True)
class Draft:
    folder: Path
    category: str
    title: str = ""
    sent: bool = False  # 슬랙 승인 요청까지 보냄 (publish.html 이 있음)
    started: float = 0.0
    skip_reason: str = ""  # 클라우드가 건너뛴 카테고리 (skip.json)


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
        if request_id_of(folder):  # 수동 지시 원고는 매일 진행 막대에 넣지 않는다 (지시 스레드에 따로 알림)
            continue
        title = str(_read(folder / "post.json").get("title") or "")
        skip = folder / "skip.json"
        reason = (str(_read(skip).get("reason") or "쓸 만한 새 소식 없음")) if skip.is_file() else ""
        found.append(Draft(folder, CATEGORIES[int(suffix) - 1], title, (folder / "publish.html").is_file(),
                           _started(folder), reason))
    return found


def _started(folder: Path) -> float:
    """폴더를 만든 시각 — 이름(YYYYMMDD_HHMMSS_n, KST)에서. 깃에서 받은 폴더는 수정 시각이 받은 시각이라 믿을 수 없다."""
    parts = folder.name.split("_")
    try:
        return datetime.strptime(f"{parts[0]}{parts[1]}", "%Y%m%d%H%M%S").replace(tzinfo=KST).timestamp()
    except (IndexError, ValueError):
        return folder.stat().st_mtime


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _record(store, draft: Draft) -> dict | None:
    """원고 보관함의 기록 — 없거나 못 읽으면 None."""
    if store is None:
        return None
    try:
        data = store.get(draft.folder.name)
    except Exception as exc:
        log.info("원고 상태 확인 실패 %s: %s", draft.folder.name, exc)
        return None
    return data if isinstance(data, dict) else None


def _delivered(record: dict | None) -> bool:
    """보관함 기록에 슬랙 메시지 위치가 있으면 승인 요청까지 끝난 원고 (없으면 슬랙 전송이 중간에 실패한 것)."""
    slack = (record or {}).get("slack")
    return isinstance(slack, dict) and bool(slack.get("ts"))


def _state(record: dict | None, draft: Draft) -> tuple[str, str]:
    """(아이콘, 설명) — 원고 보관함 기록으로 발행 여부를 본다 (기록이 없으면 승인 대기로)."""
    record = record or {}
    status, url = str(record.get("status") or "pending"), str(record.get("url") or "")
    title = escape_mrkdwn(draft.title[:60])
    if status == "published":
        return "✅", f"발행됨: <{url}|{title}>" if url else f"발행됨: {title}"
    if status == "approved":
        return "🚀", f"발행 중: {title}"
    if status == "failed":
        return "❌", f"발행 실패 (✅ 다시 누르면 재시도): {title}"
    return "📝", f"승인 대기: {title}"


def build_rows(drafts: Sequence[Draft], skipped: Mapping[str, str], store, now: float,
               failed: Mapping[str, str] | None = None, active: bool = False) -> list[Row]:
    """카테고리별 한 줄. failed = {원고 ID: 전송 실패 이유}, active = 클라우드 실행 중 (다음 카테고리를 '작성 중'으로)."""
    failed = failed or {}
    rows: list[Row] = []
    for category in CATEGORIES:
        mine = [d for d in drafts if d.category == category]
        records = {d.folder.name: _record(store, d) for d in mine if not d.skip_reason}
        sent = [d for d in mine if not d.skip_reason and (d.sent or _delivered(records[d.folder.name]))]
        skip = next((d.skip_reason for d in mine if d.skip_reason), skipped.get(category, ""))
        broken = [failed[d.folder.name] for d in mine if d.folder.name in failed]
        if sent:
            parts = [_state(records[d.folder.name], d) for d in sent]
            icon = parts[0][0]
            text = " · ".join(p[1] for p in parts)
            rows.append(Row(category, icon, text, True))
        elif skip:
            rows.append(Row(category, "⏭️", f"건너뜀 — {escape_mrkdwn(skip)}", True))
        elif broken:
            rows.append(Row(category, "⚠️", f"전송 실패 — {escape_mrkdwn(broken[0][:120])}", True))
        elif any(now - d.started < RUNNING_MINUTES * 60 for d in mine):
            rows.append(Row(category, "✍️", "작성 중", False))
        else:
            rows.append(Row(category, "⬜", "대기", False))
    if active:
        waiting = next((i for i, r in enumerate(rows) if r.icon == "⬜"), None)
        if waiting is not None and not any(r.icon == "✍️" for r in rows):
            rows[waiting] = Row(rows[waiting].category, "✍️", "작성 중 (클라우드)", False)
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

    def refresh(self, failed: Mapping[str, str] | None = None, active: bool = False) -> list[Row]:
        now = self._now()
        data = self._load()
        rows = build_rows(scan(self._drafts, now.strftime("%Y%m%d")), data.get("skipped", {}), self._store,
                          now.timestamp(), failed, active)
        text, blocks = build_blocks(rows, now.strftime("%m/%d"))
        ts = data.get("ts")
        if not (ts and self._slack.update(ts, text, blocks)):
            ts = self._slack.post(text, blocks)
        self._save({**data, "ts": ts})
        return rows
