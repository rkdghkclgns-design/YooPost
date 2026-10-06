"""(GitHub Actions) 수동 지시 원고의 진행 상황을 지시 메시지 스레드(#yoopost-0-수동지시)에 알린다.

cloud_sync 한 번 실행마다:
  • 이번에 승인 요청으로 보낸 수동 지시 원고 → "📝 원고 도착" (원고 메시지 링크)
  • 수동 지시로 건너뛴 카테고리(skip.json) → "⏭️ 건너뜀"
  • 보내지 못한 원고 → "⚠️ 전송 실패"
  • 요청 기록(runs/requests/<ID>.json)에 finished 가 찍히면 → 요청 전체 결과 한 번
  • 시작한 지 오래됐는데 끝나지 않은 요청 → "⚠️ 끝나지 않음" 한 번
같은 알림은 두 번 보내지 않는다 (state/requests.json). 원고 브랜치 데이터는 모델이 쓴 것이므로
요청 ID 가 수동 지시 채널을 가리킬 때만 글을 쓰고, 제목·이유는 모두 슬랙 표기를 이스케이프한다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .constants import CATEGORIES
from .draft_request import REQUESTS_DIR, manual_thread, request_of
from .manual_command import split_request_id
from .request_board import board_hash, board_rows, build_board, valid_ts
from .slack_helper import escape_mrkdwn

log = logging.getLogger(__name__)

STATE_FILE = "requests.json"
KEEP_DAYS = 3  # 이보다 오래된 요청의 알림 기록은 지운다
STALE_HOURS = 3  # 시작한 지 이만큼 지나도 끝나지 않으면 한 번 알린다
BOARD_HOURS = 30  # 원고 폴더를 오늘·어제 것만 보므로 이 시간 안의 요청만 진행 카드를 고친다
NOT_STARTED_MINUTES = 30  # 요청을 기록한 뒤 이만큼 지나도 루틴이 시작(request start)하지 않으면 한 번 알린다
MAX_TITLE = 120


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _category(folder: Path) -> str:
    suffix = folder.name.rsplit("_", 1)[-1]
    return CATEGORIES[int(suffix) - 1] if suffix.isdigit() and 1 <= int(suffix) <= len(CATEGORIES) else "카테고리 미정"


def _title(folder: Path) -> str:
    return escape_mrkdwn(str(_read(folder / "post.json").get("title") or folder.name)[:MAX_TITLE])


def _link(slack, channel: str, ts: str, label: str) -> str:
    url = slack.permalink(channel, ts) if channel and ts and hasattr(slack, "permalink") else ""
    return f"<{url}|{label}>" if url else label


def _status(folder: Path, outcome, records: Mapping[str, Mapping[str, object]]) -> tuple[str, str]:
    """요청 요약에 쓰는 (아이콘, 설명)."""
    name = folder.name
    if (folder / "skip.json").is_file():
        return "⏭️", f"건너뜀 — {escape_mrkdwn(str(_read(folder / 'skip.json').get('reason') or '이유 없음'))[:200]}"
    if name in records:
        return "⚠️", f"전송 실패 — {escape_mrkdwn(str(records[name].get('reason') or ''))[:200]}"
    if name in outcome.unknown or not (folder / "post.json").is_file():
        return "⏳", f"{_title(folder)} — 전송 대기"
    return "📝", f"{_title(folder)} — 승인 대기 (#yoopost-1-원고승인 에서 ✅)"


def _summary(rid: str, request: Mapping[str, object], mine: Sequence[Path], outcome, records) -> str:
    kind = str(request.get("kind") or "single")
    lines = [f"🏁 수동 지시 작업이 끝났습니다 — {'7개 카테고리' if kind == 'all' else '1편'}"]
    by_category: dict[str, list[str]] = {}
    for folder in mine:
        icon, text = _status(folder, outcome, records)
        by_category.setdefault(_category(folder), []).append(f"{icon} {text}")
    wanted = list(CATEGORIES) if kind == "all" else list(by_category) or [str(request.get("category") or "카테고리 미정")]
    for category in wanted:
        for line in by_category.get(category) or ["❌ 원고 없음"]:
            lines.append(f"• {escape_mrkdwn(category)}: {line}")
    note = str(request.get("note") or "").strip()
    if note:
        lines.append(f"📝 메모: {escape_mrkdwn(note[:300])}")
    waiting = sum(1 for line in lines if "승인 대기" in line)
    lines.append(f"승인 대기 {waiting}편 — ✅ 즉시 승인 · 1분 미승인 자동 승인 · ❌ 보류")
    return "\n".join(lines)


def _age_ok(rid: str, now: datetime) -> bool:
    place = split_request_id(rid)
    return bool(place) and now.timestamp() - float(place[1]) < KEEP_DAYS * 86400


def report_requests(root: Path, folders: Sequence[Path], outcome, records: Mapping[str, Mapping[str, object]], slack,
                    now: datetime, manual_channel: str, draft_channel: str = "",
                    lookup: Callable[[str], Mapping | None] | None = None) -> dict:
    """수동 지시 스레드 알림 + #yoopost-알림 진행 카드 — 보낸 알림 기록(state/requests.json)을 새로 써서 돌려준다.

    lookup(원고 ID) = 원고 보관함 기록 (승인 대기·발행됨 표시용, 없으면 None).
    """
    state_path = root / "state" / STATE_FILE
    old = _read(state_path)
    if not manual_channel:
        return old
    state = {rid: {"reported": list(v.get("reported") or []), "finished": str(v.get("finished") or ""),
                   "stale": bool(v.get("stale")), "board_ts": valid_ts(v.get("board_ts")),
                   "board_hash": str(v.get("board_hash") or ""), "thread_link": str(v.get("thread_link") or "")}
             for rid, v in old.items() if isinstance(v, dict) and _age_ok(rid, now)}
    per_request: dict[str, list[Path]] = {}
    for folder in folders:
        rid = request_of(folder).get("id", "")
        place = manual_thread(rid, manual_channel)
        if not place:
            continue
        per_request.setdefault(rid, []).append(folder)
        entry = state.setdefault(rid, _new_entry())
        category = escape_mrkdwn(_category(folder))
        # 알림은 종류별로 한 번씩 — 전송 실패를 알린 원고가 나중에 재시도로 도착하면 '원고 도착'도 단다
        if folder.name in outcome.sent:
            event = "arrived"
            link = _link(slack, draft_channel, outcome.messages.get(folder.name, ""), "원고 메시지 열기")
            text = f"📨 원고 도착 — *{category}* · {_title(folder)}\n#yoopost-1-원고승인: ✅ 즉시 승인 · 1분 미승인 자동 승인 · ❌ 보류 · {link}"
        elif (folder / "skip.json").is_file():
            event = "skipped"
            reason = escape_mrkdwn(str(_read(folder / "skip.json").get("reason") or "이유 없음"))[:300]
            text = f"⏭️ *{category}* 원고를 쓰지 못했습니다 — {reason}"
        elif folder.name in outcome.new_failures:
            event = "failed"
            text = f"⚠️ *{category}* 원고를 승인 요청으로 보내지 못했습니다 — {escape_mrkdwn(outcome.new_failures[folder.name])}"
        else:
            continue
        key = f"{event}:{folder.name}"
        if key in entry["reported"]:
            continue
        slack.post(text, thread_ts=place[1], channel=place[0])
        entry["reported"].append(key)
    for path in sorted((root / "runs" / REQUESTS_DIR).glob("*.json")):
        rid = path.stem
        place = manual_thread(rid, manual_channel)
        if not place or not _age_ok(rid, now):
            continue
        request = _read(path)
        entry = state.setdefault(rid, _new_entry())
        try:  # 진행 카드는 스레드 알림과 따로 — 실패해도 스레드 알림은 계속
            _refresh_board(slack, request, per_request.get(rid, []), lookup, records, entry, place, now)
        except Exception as exc:
            log.warning("수동 지시 진행 카드를 고치지 못했습니다 %s: %s", rid, exc)
        if request.get("finished") and not entry["finished"]:
            slack.post(_summary(rid, request, per_request.get(rid, []), outcome, records),
                       thread_ts=place[1], channel=place[0])
            entry["finished"] = str(request["finished"])
            continue
        if request.get("finished") or request.get("fire_error") or entry["stale"]:
            continue  # 끝났거나, 루틴 실행 실패로 닫힌 요청(그 자리에서 이미 알림)이거나, 이미 경고함
        started = _parse(str(request.get("started") or ""))
        created = _parse(str(request.get("created") or ""))
        if started is None and created is not None and now.timestamp() - created.timestamp() > NOT_STARTED_MINUTES * 60:
            slack.post(f"⚠️ 지시를 받은 지 {NOT_STARTED_MINUTES}분이 지나도 원고 작성이 시작되지 않았습니다 — "
                       "claude.ai/code 에서 'YooPost 수동 지시 (슬랙)' 실행 기록을 확인하거나 다시 지시해 주세요",
                       thread_ts=place[1], channel=place[0])
            entry["stale"] = True
        elif started is not None and now.timestamp() - started.timestamp() > STALE_HOURS * 3600:
            slack.post(f"⚠️ 수동 지시 작업이 {STALE_HOURS}시간이 지나도 끝나지 않았습니다 — "
                       "claude.ai/code 에서 'YooPost 수동 지시 (슬랙)' 실행 결과를 확인해 주세요",
                       thread_ts=place[1], channel=place[0])
            entry["stale"] = True
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return state


def _new_entry() -> dict:
    return {"reported": [], "finished": "", "stale": False, "board_ts": "", "board_hash": "", "thread_link": ""}


def _refresh_board(slack, request: Mapping, folders: Sequence[Path], lookup, records, entry: dict,
                   place: tuple[str, str], now: datetime) -> None:
    """#yoopost-알림 진행 카드를 새 상태로 — 내용이 바뀌었을 때만 고치고, 카드가 없으면 새로 올린다.

    원고 폴더를 오늘·어제 것만 보므로 그보다 오래된 요청의 카드는 더 고치지 않는다 (마지막 상태로 둔다).
    """
    created = _parse(str(request.get("created") or ""))
    if created is None or now.timestamp() - created.timestamp() > BOARD_HOURS * 3600:
        return
    if not entry["thread_link"] and hasattr(slack, "permalink"):
        entry["thread_link"] = slack.permalink(place[0], place[1])
    text, blocks = build_board(request, board_rows(request, folders, lookup, records), entry["thread_link"])
    mark = board_hash(text, blocks)
    if mark == entry["board_hash"]:
        return
    ts = entry["board_ts"] or valid_ts(request.get("alert_ts"))
    if ts and slack.update(ts, text, blocks):
        entry["board_ts"], entry["board_hash"] = ts, mark
    elif not ts and not request.get("fire_error"):  # 실행 실패로 닫힌 요청은 접수할 때 이미 알렸다
        posted = slack.post(text, blocks)
        if posted:
            entry["board_ts"], entry["board_hash"] = str(posted), mark


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
