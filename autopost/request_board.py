"""수동 지시 진행 카드 — #yoopost-알림 에 요청마다 메시지 1개를 올리고 단계가 바뀔 때마다 고친다.

    🧑‍💼 [AI백과사전] 수동 지시 — 1편 · AI 도구   ▰▰▱▱ 2/4 작성·검증 중
    지시: «슬랙에 에이전트 직원 만들기» · <@U…> · 09/30 11:14 · 지시 스레드
    1. ✍️ AI 도구 — 작성·검증 중 (Sonnet 작성 → Opus 사실 검증)

단계: 접수 → 작성·검증 → 승인 대기(#yoopost-1-원고승인) → 발행(✅). '전체' 지시는 카테고리 7줄과 끝난 수/7.
카드는 manual_request.yml 이 접수할 때 올리고(요청 기록 alert_ts), cloud_sync 가 원고 브랜치·원고 보관함 상태로 고친다.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .constants import CATEGORIES
from .slack_helper import AUTO_APPROVAL_LABEL, LABEL, escape_mrkdwn

STAGES = ("접수", "작성·검증", "승인 대기", "발행")
BAR_ON, BAR_OFF = "▰", "▱"
TS = re.compile(r"^\d{10}\.\d{6}$")


@dataclass(frozen=True)
class BoardRow:
    category: str
    icon: str
    text: str
    stage: int  # 0~4 (STAGES 몇 단계까지 왔는지) — 건너뜀·실패는 끝난 것으로 4
    finished: bool


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _title(folder: Path) -> str:
    return escape_mrkdwn(str(_read(folder / "post.json").get("title") or folder.name)[:60])


def _category_of(folder: Path) -> str:
    suffix = folder.name.rsplit("_", 1)[-1]
    return CATEGORIES[int(suffix) - 1] if suffix.isdigit() and 1 <= int(suffix) <= len(CATEGORIES) else ""


def _draft_row(category: str, folder: Path, record: Mapping | None, failures: Mapping[str, Mapping]) -> BoardRow:
    title = _title(folder)
    if (folder / "skip.json").is_file():
        reason = escape_mrkdwn(str(_read(folder / "skip.json").get("reason") or "이유 없음"))[:120]
        return BoardRow(category, "⏭️", f"건너뜀 — {reason}", 4, True)
    status = str((record or {}).get("status") or "")
    approval = f"{AUTO_APPROVAL_LABEL} · " if (record or {}).get("approved_by") == "auto-60s" else ""
    slack = (record or {}).get("slack")
    delivered = isinstance(slack, Mapping) and bool(slack.get("ts"))
    if status == "published":
        url = str((record or {}).get("url") or "")
        return BoardRow(category, "✅", f"{approval}발행됨: <{url}|{title}>" if url else f"{approval}발행됨: {title}", 4, True)
    if status == "excluded":  # 이미 쓴 글과 겹쳐 자동 제외 (✅ 를 눌러도 발행하지 않음)
        return BoardRow(category, "🚫", f"겹침으로 제외: {title}", 4, True)
    if status == "approved":
        return BoardRow(category, "🚀", f"{approval}발행 중: {title}", 3, False)
    if status == "held":
        return BoardRow(category, "⏸️", f"보류 (✅ 승인하면 발행): {title}", 3, False)
    if status == "failed":
        return BoardRow(category, "❌", f"{approval}발행 실패 (✅ 다시 누르면 재시도): {title}", 3, False)
    if delivered:
        return BoardRow(category, "📝", f"승인 대기: {title} — #yoopost-1-원고승인 에서 ✅", 3, False)
    if folder.name in failures:
        reason = escape_mrkdwn(str(failures[folder.name].get("reason") or ""))[:120]
        return BoardRow(category, "⚠️", f"승인 요청 전송 실패 — {reason}", 2, False)
    return BoardRow(category, "📨", f"원고 전송 중: {title}", 2, False)


def board_rows(request: Mapping, folders: Sequence[Path], lookup: Callable[[str], Mapping | None] | None,
               failures: Mapping[str, Mapping]) -> list[BoardRow]:
    """요청의 카테고리별 한 줄 (folders = 이 요청의 원고 폴더들)."""
    kind = str(request.get("kind") or "single")
    wanted = list(CATEGORIES) if kind == "all" else [str(request.get("category") or "") or "카테고리 미정"]
    started, finished = bool(request.get("started")), bool(request.get("finished"))
    fallback = request.get("fallback") or {}
    cloud = fallback.get("provider") == "codex" and fallback.get("mode") == "cloud"
    active_category = fallback.get("category") if cloud else ""
    rows: list[BoardRow] = []
    writing_marked = False
    for category in wanted:
        mine = [f for f in folders if _category_of(f) == category or (kind == "single" and not request.get("category"))]
        if request.get("fire_error"):
            rows.append(BoardRow(category, "❌", f"원고 작성을 시작하지 못함 — {escape_mrkdwn(str(request['fire_error']))[:120]}", 4, True))
            continue
        if mine:
            folder = mine[-1]
            record = None
            if lookup is not None and not (folder / "skip.json").is_file():
                try:
                    record = lookup(folder.name)
                except Exception:
                    record = None
            rows.append(_draft_row(category, folder, record, failures))
        elif finished:
            rows.append(BoardRow(category, "❌", "원고 없음", 4, True))
        elif cloud and fallback.get("status") == "waiting":
            failed = fallback.get("last_result") == "failed" and fallback.get("category") == category
            detail = escape_mrkdwn(str(request.get("note") or ""))[:160]
            rows.append(BoardRow(category, "⚠️" if failed else "⬜",
                                 f"작성 중단 — {detail} (재시도 대기)" if failed else "Codex 클라우드 순차 처리 대기", 1, False))
        elif started and not writing_marked and (not active_category or category == active_category):
            author = "Codex 클라우드 작성 → 별도 사실 검증" if fallback.get("provider") == "codex" else "Sonnet 작성 → Opus 사실 검증"
            rows.append(BoardRow(category, "✍️", f"작성·검증 중 ({author})", 1, False))
            writing_marked = True
        elif started:
            rows.append(BoardRow(category, "⬜", "대기", 1, False))
        else:
            rows.append(BoardRow(category, "⏳", "접수 — 클라우드 루틴 시작 대기", 0, False))
    return rows


def _bar(done: int, total: int) -> str:
    return BAR_ON * done + BAR_OFF * (total - done) + f" {done}/{total}"


def build_board(request: Mapping, rows: Sequence[BoardRow], thread_link: str = "") -> tuple[str, list[dict]]:
    """(알림 글, 블록) — 한 편이면 단계 막대, 전체면 카테고리 막대."""
    kind = str(request.get("kind") or "single")
    if kind == "all":
        done = sum(1 for r in rows if r.finished or r.stage >= 3)
        what = "7개 카테고리 새 글"
        progress = _bar(done, len(rows))
    else:
        row = rows[0] if rows else BoardRow("", "⏳", "", 0, False)
        what = f"1편 · {escape_mrkdwn(row.category)}"
        step, label = _single_step(row)
        progress = f"{_bar(step, len(STAGES))} {label}"
    head = f"🧑‍💼 {LABEL} 수동 지시 — {what}  {progress}"
    topic = str(request.get("topic") or "")
    user = str(request.get("requested_by") or "")
    when = _when(str(request.get("created") or ""))
    parts = [f"지시: «{escape_mrkdwn(topic[:120])}»" if topic else ("지시: 새 주제로" if kind == "single" else "지시: 전체 새 글")]
    if re.fullmatch(r"U[A-Z0-9]{4,20}", user):
        parts.append(f"<@{user}>")
    if when:
        parts.append(when)
    if thread_link:
        parts.append(f"<{thread_link}|지시 스레드>")
    lines = "\n".join(f"{i}. {r.icon} *{escape_mrkdwn(r.category)}* — {r.text}" for i, r in enumerate(rows, start=1))
    waiting = sum(1 for r in rows if r.icon == "📝")
    footer = (f"승인 대기 {waiting}편 — #yoopost-1-원고승인: ✅ 즉시 승인 · 1분 미승인 자동 승인 · ❌ 보류"
              if waiting else "원고는 사실 검증 뒤 #yoopost-1-원고승인 에 올라옵니다. ✅ 즉시 승인 · 1분 미승인 자동 승인 · ❌ 보류")
    return head, [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{head}*\n{' · '.join(parts)}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": lines[:2900] or "-"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]},
    ]


_STEP_LABEL = {"⏳": (1, "접수"), "✍️": (2, "작성·검증 중"), "⬜": (2, "작성·검증 중"), "📨": (2, "원고 전송 중"),
               "⚠️": (2, "승인 요청 전송 실패"), "📝": (3, "승인 대기"), "🚀": (3, "발행 중"), "✅": (4, "발행됨")}


def _single_step(row: BoardRow) -> tuple[int, str]:
    """한 편 지시의 (막대 칸 수, 단계 이름) — 건너뜀·원고 없음·시작 실패는 끝난 것으로 4칸."""
    if row.icon == "⚠️" and row.text.startswith("작성 중단"):
        return 2, "작성 중단 · 재시도 대기"
    if row.icon == "⬜" and "순차 처리 대기" in row.text:
        return 2, "순차 처리 대기"
    if row.icon == "❌" and not row.finished:
        return 3, "발행 실패"
    if row.finished and row.icon != "✅":
        return 4, row.text.split(" — ")[0]
    return _STEP_LABEL.get(row.icon, (1, "접수"))


def board_hash(text: str, blocks: list[dict]) -> str:
    return hashlib.sha256(json.dumps([text, blocks], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def valid_ts(value: object) -> str:
    return str(value) if TS.match(str(value or "")) else ""


def _when(created: str) -> str:
    try:
        return datetime.fromisoformat(created).strftime("%m/%d %H:%M")
    except ValueError:
        return ""
