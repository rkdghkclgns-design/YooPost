"""수동 지시(#yoopost-0-수동지시)로 쓴 원고 표시 — 매일 07시 원고와 구분한다.

원고 폴더의 research.json(원고) 또는 skip.json(건너뜀)에 {"request": {"id": 요청 ID, "kind": "single"|"all"}} 가 있으면
수동 지시 원고다. 이 표시는 클라우드 도우미(scripts/cloud_drafts.py push/skip --request)가 붙인다 (모델이 쓰지 않음).
수동 지시 원고는 매일 7개 카테고리 빈칸 계산·진행 막대에 넣지 않고, 지시 메시지 스레드에 따로 알린다.
요청 기록은 원고 브랜치 runs/requests/<요청 ID>.json {"id","kind","category","topic","started","finished","note"}.
"""
from __future__ import annotations

import json
from pathlib import Path

from .manual_command import KINDS, split_request_id

REQUEST_KEY = "request"
REQUESTS_DIR = "requests"  # runs/ 아래 — 원고 브랜치 push 알림(runs/**)이 그대로 Actions 를 깨운다
MAX_AGE_HOURS = 6  # 이보다 오래된 요청은 루틴이 시작하지 않는다 (늦게 온 중복 실행 방지)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def request_of(folder: Path) -> dict[str, str]:
    """원고 폴더의 수동 지시 표시 {"id","kind"} — 없거나 형식이 틀리면 빈 dict (= 매일 원고)."""
    for name in ("research.json", "skip.json"):
        raw = _read(folder / name).get(REQUEST_KEY)
        if isinstance(raw, dict) and split_request_id(str(raw.get("id") or "")):
            kind = str(raw.get("kind") or "single")
            return {"id": str(raw["id"]), "kind": kind if kind in KINDS else "single"}
    return {}


def request_id_of(folder: Path) -> str:
    return request_of(folder).get("id", "")


def with_request(data: dict, rid: str, kind: str) -> dict:
    """research.json·skip.json 내용에 수동 지시 표시를 붙인 새 dict."""
    if not split_request_id(rid):
        raise ValueError(f"요청 ID 형식이 아닙니다: {rid!r}")
    if kind not in KINDS:
        raise ValueError(f"지시 종류는 {', '.join(KINDS)} 중 하나: {kind!r}")
    return {**data, REQUEST_KEY: {"id": rid, "kind": kind}}


def request_path(tree: Path, rid: str) -> Path:
    """원고 브랜치 작업 폴더 안의 요청 기록 파일 (요청 ID 형식이 아니면 ValueError — 경로 조작 방지)."""
    if not split_request_id(rid):
        raise ValueError(f"요청 ID 형식이 아닙니다: {rid!r}")
    return tree / "runs" / REQUESTS_DIR / f"{rid}.json"


def read_request(tree: Path, rid: str) -> dict:
    """요청 기록 (없거나 형식이 틀리면 빈 dict)."""
    try:
        return _read(request_path(tree, rid))
    except ValueError:
        return {}


def new_record(rid: str, kind: str, category: str, topic: str, user: str, created: str) -> dict:
    """Actions(manual_request.yml)가 루틴을 깨우기 전에 원고 브랜치에 남기는 요청 기록 — 루틴은 이 기록과 맞는 요청만 한다."""
    if kind not in KINDS:
        raise ValueError(f"지시 종류는 {', '.join(KINDS)} 중 하나: {kind!r}")
    request_path(Path("."), rid)  # 형식 확인
    return {"id": rid, "kind": kind, "category": category if kind == "single" else "", "topic": topic if kind == "single" else "",
            "requested_by": user, "created": created, "started": "", "network": "", "finished": "", "note": ""}


def manual_thread(rid: str, manual_channel: str) -> tuple[str, str] | None:
    """요청 ID → (채널, 스레드 ts). 수동 지시 채널이 아니면 None (원고 브랜치 데이터로 다른 채널에 글을 쓰지 않도록)."""
    place = split_request_id(rid)
    return place if place and manual_channel and place[0] == manual_channel else None
