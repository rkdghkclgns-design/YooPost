"""슬랙 알림/리포트 (YooPost 워크스페이스).

- 슬랙 오류가 자동화 전체를 멈추지 않도록 모든 예외를 삼킨 뒤 로그로 남긴다.
- 외부 라이브러리 오류 문구에 API 키·토큰이 섞여 나갈 수 있어, 전송 직전 모든 텍스트에서 비밀값을 가린다.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable, Sequence

from .results import PublishResult

log = logging.getLogger(__name__)

LABEL = "[AI백과사전]"
AUTO_APPROVAL_LABEL = "🤖✅ 자동 승인 (1분 경과)"
MAX_TEXT = 2900  # 슬랙 section 블록 텍스트 한도(3000) 여유
MASK = "***"
MIN_SECRET_LEN = 6
# (패턴, 치환문자열) — 그룹 1 은 남기고 비밀값 부분만 가린다
SECRET_PATTERNS = (
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), MASK),  # Google API 키 (Gemini, 예전 형식)
    (re.compile(r"\bAQ\.[0-9A-Za-z_\-.]{30,}"), MASK),  # Google API 키 (새 형식)
    (re.compile(r"ya29\.[0-9A-Za-z_\-.]+"), MASK),  # Google 액세스 토큰
    (re.compile(r"xox[abposre]-[0-9A-Za-z\-]+"), MASK),  # 슬랙 토큰
    (re.compile(r"(?:ghp|gho|ghs|ghu|github_pat)_[0-9A-Za-z_]+"), MASK),  # GitHub 토큰
    (re.compile(r"sk-ant-[0-9A-Za-z_\-]+"), MASK),  # Anthropic 토큰 (클라우드 루틴 실행 토큰 sk-ant-oat01-…)
    (re.compile(r"([?&](?:key|access_token|refresh_token|token)=)[^&\s\"'<>|]+"), r"\g<1>" + MASK),
    (re.compile(r"(Bearer )[0-9A-Za-z_\-.~+/=]{8,}"), r"\g<1>" + MASK),
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """알려진 비밀값과 흔한 토큰 패턴을 '***' 로 치환."""
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= MIN_SECRET_LEN:
            text = text.replace(secret, MASK)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def escape_mrkdwn(text: str) -> str:
    """외부 입력을 슬랙 mrkdwn 에 넣기 전 이스케이프 (<!channel> 멘션·링크·코드블록 탈출 방지)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "'")


def _truncate(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _status_icon(result: PublishResult) -> str:
    if result.ok:
        return "✅"
    return "⏭️" if result.skipped else "❌"


def build_report_blocks(
    title: str,
    category: str,
    results: Sequence[PublishResult],
    fact_line: str,
    dry_run: bool = False,
    held: bool = False,
    usage_line: str = "",
) -> list[dict]:
    if held:
        header = f"⚠️ {LABEL} 사실 검증 미통과 — 발행 보류"
    else:
        header = f"🧪 {LABEL} 테스트 실행 결과" if dry_run else f"🤖 {LABEL} 포스팅 완료 리포트"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(
            f"*제목:* {escape_mrkdwn(title)}\n*카테고리:* {escape_mrkdwn(category)}\n*사실 검증:* {escape_mrkdwn(fact_line)}"
        )}},
        {"type": "divider"},
    ]
    for result in results:
        line = f"{_status_icon(result)} *{result.platform}*"
        if result.url:
            line += f"  <{result.url}|게시글 열기>"
        if result.message:
            line += f"\n{escape_mrkdwn(result.message)}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _truncate(line)}})
    footer = ("원고·사실 검증 보고는 원고 채널에서 확인하고, 고친 뒤 직접 발행하거나 다른 주제로 다시 실행하세요"
              if held else "출처·사실 검증 보고는 원고 채널(#yoopost-1-원고승인)에 올라가 있습니다")
    if usage_line:
        footer += f"\n📊 {escape_mrkdwn(usage_line)}"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


def build_setup_blocks(checks: Sequence[tuple[str, bool, str]], blog_url: str = "") -> list[dict]:
    """/aipost-setup 연동 점검 결과."""
    ok = all(passed for _, passed, _ in checks)
    checklist = "\n".join(
        f"{'✅' if passed else '⬜'} {name}" + ("" if passed else f"  _(필요: {escape_mrkdwn(hint)})_")
        for name, passed, hint in checks
    )
    header = f"✅ {LABEL} 연동 점검 완료" if ok else f"⚠️ {LABEL} 연동 점검 — 확인 필요"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(f"*연동 점검*\n{checklist}")}},
    ]
    footer = "원고는 Claude 앱이 쓰고, 슬랙 원고 메시지에 ✅ 를 누르면 발행됩니다 (구글 AI API·드라이브 미사용)"
    if blog_url:
        footer += f"  |  <{blog_url}|구글 블로그 열기>"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


class SlackNotifier:
    def __init__(
        self,
        bot_token: str = "",
        channel_id: str = "",
        webhook_url: str = "",
        client=None,
        webhook=None,
        secrets: Iterable[str] = (),
    ) -> None:
        self._channel_id = channel_id
        self._client = client
        self._webhook = webhook
        self._secrets = tuple(s for s in secrets if s)
        if self._client is None and bot_token and channel_id:
            from slack_sdk import WebClient

            self._client = WebClient(token=bot_token)
        if self._webhook is None and webhook_url:
            from slack_sdk.webhook import WebhookClient

            self._webhook = WebhookClient(webhook_url)

    @property
    def enabled(self) -> bool:
        return self._client is not None or self._webhook is not None

    def _clean(self, text: str) -> str:
        return redact(text, self._secrets)

    def _clean_blocks(self, blocks: list[dict] | None) -> list[dict] | None:
        if blocks is None:
            return None
        return json.loads(self._clean(json.dumps(blocks, ensure_ascii=False)))

    @property
    def can_thread(self) -> bool:
        """메시지 수정·스레드·파일 업로드는 봇 토큰이 있어야 가능 (웹훅은 새 메시지만)."""
        return self._client is not None

    def post(
        self,
        text: str,
        blocks: list[dict] | None = None,
        thread_ts: str | None = None,
        channel: str | None = None,
    ) -> str | None:
        """봇으로 보내고(성공 시 메시지 ts 반환), 봇이 없거나 실패하면 웹훅으로 보낸다."""
        text, blocks = self._clean(text), self._clean_blocks(blocks)
        log.info("[슬랙] %s", text)
        if self._client is not None:
            try:
                response = self._client.chat_postMessage(
                    channel=channel or self._channel_id, text=text, blocks=blocks, thread_ts=thread_ts,
                    unfurl_links=False,
                )
                return (response or {}).get("ts")
            except Exception as exc:
                log.error("슬랙 봇 전송 실패: %s%s", exc, " — 웹훅으로 재시도" if self._webhook else "")
        if self._webhook is not None:
            try:
                response = self._webhook.send(text=text, blocks=blocks)
                status = getattr(response, "status_code", 200)
                if status != 200:
                    log.error("슬랙 웹훅 전송 실패: HTTP %s", status)
            except Exception as exc:
                log.error("슬랙 웹훅 전송 실패: %s", exc)
        return None

    def update(self, ts: str, text: str, blocks: list[dict] | None = None, channel: str | None = None) -> bool:
        """이미 보낸 메시지를 고친다 (진행 상황 체크리스트 갱신용)."""
        if self._client is None or not ts:
            return False
        try:
            self._client.chat_update(
                channel=channel or self._channel_id, ts=ts, text=self._clean(text), blocks=self._clean_blocks(blocks)
            )
            return True
        except Exception as exc:
            log.error("슬랙 메시지 수정 실패: %s", exc)
            return False

    def permalink(self, channel: str, ts: str) -> str:
        """메시지 주소 (봇 토큰이 없거나 실패하면 빈 문자열)."""
        if self._client is None or not channel or not ts:
            return ""
        try:
            return str((self._client.chat_getPermalink(channel=channel, message_ts=ts) or {}).get("permalink") or "")
        except Exception as exc:
            log.warning("슬랙 메시지 주소를 얻지 못했습니다: %s", exc)
            return ""

    def upload_file(
        self,
        path: Path,
        title: str,
        comment: str = "",
        thread_ts: str | None = None,
        channel: str | None = None,
    ) -> bool:
        if self._client is None:
            log.info("[슬랙] 봇 토큰이 없어 파일 업로드를 건너뜁니다: %s", path.name)
            return False
        try:
            self._client.files_upload_v2(
                channel=channel or self._channel_id, file=str(path), title=title,
                initial_comment=self._clean(comment), thread_ts=thread_ts,
            )
            return True
        except Exception as exc:
            log.error("슬랙 파일 업로드 실패: %s", exc)
            return False

    def notify_error(self, stage: str, error: Exception | str) -> None:
        detail = escape_mrkdwn(_truncate(str(error), 1500))
        self.post(f"🚨 {LABEL} '{stage}' 단계에서 오류가 발생했습니다.\n```{detail}```")
