"""슬랙 수동 지시 → 클라우드 루틴 실행 (GitHub Actions manual_request.yml 에서 호출).

Worker 가 #yoopost-0-수동지시 메시지(또는 /aipost 명령)를 받아 이 단계를 실행시킨다:
  지시 해석 → 그 메시지 스레드에 접수 답장 → 루틴 API(/fire)로 원고 작성 세션 시작 → 세션 링크 답장.
원고는 루틴이 원고 브랜치에 올리고, 여느 원고처럼 #yoopost-1-원고승인 에서 ✅ 로 발행된다.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .draft_request import new_record
from .manual_command import NO_VERB, USAGE, ManualCommand, fire_text, parse_command, request_id
from .request_board import board_rows, build_board
from .slack_helper import SlackNotifier, escape_mrkdwn

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
FIRE_URL = re.compile(r"^https://api\.anthropic\.com/v1/claude_code/routines/trig_[A-Za-z0-9]+/fire$")
FIRE_TIMEOUT = 30
SETUP_HINT = ("⚙️ 수동 지시 설정이 아직 끝나지 않았습니다 — 루틴 'YooPost 수동 지시 (슬랙)'의 API 토큰을 "
              "GitHub production 환경 비밀값 `ROUTINE_FIRE_TOKEN` 에, 주소를 변수 `ROUTINE_FIRE_URL` 에 등록해 주세요.")


@dataclass(frozen=True)
class FireResult:
    ok: bool
    session_url: str = ""
    error: str = ""


def valid_fire_url(url: str) -> bool:
    """토큰을 루틴 실행 주소 말고 다른 곳으로 보내지 않는다."""
    return bool(FIRE_URL.match(url or ""))


def _error_message(response) -> str:
    try:
        return str(((response.json() or {}).get("error") or {}).get("message") or "")[:200]
    except Exception:
        return ""


def fire_routine(url: str, token: str, text: str, post=None, wait: Callable[[float], None] = time.sleep) -> FireResult:
    """루틴 API 로 새 세션을 시작한다. 성공하면 세션 주소."""
    if not valid_fire_url(url):
        return FireResult(False, error="루틴 실행 주소(ROUTINE_FIRE_URL) 형식이 올바르지 않습니다")
    if post is None:
        import requests

        post = requests.post
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "experimental-cc-routine-2026-04-01",
        "Content-Type": "application/json",
    }
    try:
        response = post(url, headers=headers, json={"text": text}, timeout=FIRE_TIMEOUT)
    except Exception as exc:  # 응답을 못 받았으면 세션이 이미 만들어졌을 수 있어 다시 보내지 않는다
        return FireResult(False, error=f"응답 없음({type(exc).__name__}) — 클라우드 점검에서 실제 시작 여부를 확인합니다")
    status = response.status_code
    if status >= 500:
        # fire에는 멱등키가 없다. 서버 오류 응답도 생성 실패를 확정할 수 없으므로 재전송하지 않는다.
        return FireResult(False, error=f"응답 없음(HTTP {status}) — 클라우드 점검에서 실제 시작 여부를 확인합니다")
    if status == 200:
        try:
            body = response.json() or {}
            return FireResult(True, str(body.get("claude_code_session_url") or ""))
        except (ValueError, TypeError, AttributeError):
            return FireResult(False, error="응답 없음(잘못된 성공 응답) — 클라우드 점검에서 실제 시작 여부를 확인합니다")
    detail = _error_message(response)
    if status == 401:
        return FireResult(False, error="루틴 토큰이 맞지 않습니다 (ROUTINE_FIRE_TOKEN 을 새로 등록해 주세요)")
    if status == 429:
        retry = response.headers.get("Retry-After", "")
        return FireResult(False, error=f"한 시간 실행 한도에 걸렸습니다{f' ({retry}초 뒤 다시)' if retry else ''}")
    if status == 400:
        return FireResult(False, error=f"루틴이 요청을 거절했습니다 (꺼져 있는지 확인): {detail or 'HTTP 400'}")
    return FireResult(False, error=f"루틴 실행 실패 HTTP {status}{f' — {detail}' if detail else ''}")


def describe(command: ManualCommand) -> str:
    if command.kind == "all":
        return "7개 카테고리 새 글 1편씩 (7편)"
    category = command.category or "Claude 가 고름"
    topic = escape_mrkdwn(command.topic) if command.topic else "Claude 가 새로 고름"
    return f"1편 · 카테고리 *{escape_mrkdwn(category)}* · 주제 *{topic}*"


def handle_request(
    text: str,
    channel: str,
    ts: str,
    user: str,
    source: str,
    *,
    manual_channel: str,
    notifier: SlackNotifier,
    fire: Callable[[str], FireResult] | None,
    record: Callable[[dict], None] | None = None,
    now: datetime | None = None,
    alert_channel: str = "",
    fallback: Callable[[str], bool] | None = None,
    watch: Callable[[str, str, str], bool] | None = None,
    delivery_id: str = "",
) -> str:
    """지시 하나를 처리하고 결과 상태(fired / help / invalid / ignored / not configured / failed)를 돌려준다.

    record(요청 기록) — 루틴을 깨우기 전에 원고 브랜치에 요청을 남긴다 (루틴은 이 기록과 맞는 요청만 수행).
    alert_channel(#yoopost-알림) — 수동 지시 진행 카드를 올린다 (이후 cloud_sync 가 단계마다 고친다).
    """
    if source == "slash":  # /aipost 는 어느 채널에서든 — 수동 지시 채널에 새 글타래를 연다
        root = notifier.post(f"🧑‍💼 <@{user}> 지시 (`/aipost`): {escape_mrkdwn(text)}", channel=manual_channel)
        if not root:
            return "failed"
        channel, ts = manual_channel, root
    elif channel != manual_channel or not ts:
        return "ignored"

    def reply(message: str) -> None:
        notifier.post(message, thread_ts=ts, channel=channel)

    command = parse_command(text)
    if command.kind == "help":
        reply(USAGE)
        return "help"
    if command.kind == "invalid":  # 잡담(동사 없음)에는 짧게, 알아듣지 못한 지시에는 사용법까지
        reply(f"🤔 {command.error}" if command.error == NO_VERB else f"🤔 {command.error}\n\n{USAGE}")
        return "invalid"
    rid = request_id(channel, ts)
    created = (now or datetime.now(timezone.utc)).astimezone(KST).isoformat(timespec="seconds")
    request = new_record(rid, command.kind, command.category, command.topic, user, created)
    if delivery_id:
        request["delivery_id"] = delivery_id
    thread_link = notifier.permalink(channel, ts) if alert_channel else ""

    def board(state: dict) -> tuple[str, list[dict]]:
        return build_board(state, board_rows(state, [], None, {}), thread_link)

    if alert_channel:  # #yoopost-알림 에 진행 카드 — 위치를 요청 기록에 남겨 이후 단계마다 고친다
        alert_ts = notifier.post(*board(request), channel=alert_channel)
        if alert_ts:
            request = {**request, "alert_ts": alert_ts}
    if record is not None:
        try:
            record(request)
        except Exception as exc:
            log.error("요청 기록 실패: %s", exc)
            reply("❌ 요청을 원고 브랜치에 기록하지 못해 작성을 시작하지 않았습니다 — 잠시 뒤 다시 지시해 주세요.")
            return "failed"
    watch_notice = ""
    if watch is not None:
        try:
            if watch(rid, created, user):
                watch_notice = "\n⏱️ 15분 동안 작성이 시작되지 않으면 Codex 클라우드가 다시 확인해 이어받습니다."
            else:
                log.warning("미시작 인계 타이머 등록 실패 — 정기 클라우드 점검에서 다시 확인합니다")
        except Exception:
            log.warning("미시작 인계 타이머 연결 실패 — 정기 클라우드 점검에서 다시 확인합니다")
    result = fire(fire_text(command, rid)) if fire else FireResult(False, error="Claude 루틴 설정 누락")
    if not result.ok:
        closed = {**request, "fire_error": result.error[:200]}
        saved = record is None
        if record is not None:
            try:  # 닫힌 요청으로 표시 — 루틴이 늦게 떠도 하지 않고, '시작되지 않았습니다' 경고도 다시 달지 않는다
                record(closed)
                saved = True
            except Exception as exc:
                log.warning("요청 기록에 실행 실패를 남기지 못했습니다: %s", exc)
        if request.get("alert_ts"):
            notifier.update(str(request["alert_ts"]), *board(closed), channel=alert_channel)
        if saved and fallback is not None and "응답 없음" not in result.error and fallback(rid):
            reply("🤖 Claude 실행을 계속할 수 없어 *Codex 클라우드에 인계를 요청했습니다*. "
                  "PC를 꺼도 GitHub에서 이어서 작성·검증합니다. 인계가 시작되면 이 스레드에 알려 드립니다. "
                  "원고는 #yoopost-1-원고승인 에서 ✅ 즉시 승인 또는 1분 미승인 자동 승인으로 발행됩니다. ❌는 보류입니다.")
            return "fallback"
        reply(SETUP_HINT if fire is None else
              f"❌ 원고 작성을 시작하지 못했습니다: {escape_mrkdwn(result.error)}\n요청은 보존했으며 클라우드 점검에서 다시 확인합니다.")
        return "failed" if fire else "not configured"
    link = f" · <{result.session_url}|작성 과정 보기>" if result.session_url else ""
    reply(f"📝 접수했습니다 — {describe(command)}{link}\n"
          "Claude 가 조사·작성하고 Opus 가 사실 검증한 원고가 #yoopost-1-원고승인 에 올라오면 이 스레드에 알려 드립니다. "
          "✅ 즉시 승인 또는 1분 미승인 자동 승인으로 발행됩니다. ❌는 보류입니다." + watch_notice)
    return "fired"
