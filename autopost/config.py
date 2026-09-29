"""환경변수 로딩 및 검증.

발행 대상은 구글 블로그 하나다 (네이버는 쓰지 않음). BLOGGER_BLOG_ID 가 없으면 포스팅 모드는 시작하지 않는다.
자료 조사는 Gemini + 구글 검색 근거(무료), 이미지는 이미지 저장소(Cloudflare Worker) — 구글 드라이브는 쓰지 않는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .constants import DEFAULT_BLOG_NAME

MODES = ("post", "setup")
# 최신 flash 를 자동으로 가리키는 별칭 (특정 모델 은퇴에 영향받지 않음)
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
# 기본 모델이 과부하(503)·중단(404)일 때 순서대로 시도
DEFAULT_GEMINI_FALLBACKS = "gemini-3.8-flash,gemini-3.5-flash,gemini-3-flash-preview,gemini-2.5-flash"
# 진행 단계별 슬랙 채널 (비우면 그 단계 결과물은 알림 채널의 진행 메시지 스레드에 올라감)
STAGE_CHANNEL_ENV = (
    ("research", "SLACK_CHANNEL_RESEARCH"),  # 주제·출처 목록
    ("draft", "SLACK_CHANNEL_DRAFT"),  # AI 원고·사실 검증 보고·블로그 미리보기
    ("publish", "SLACK_CHANNEL_PUBLISH"),  # 발행 결과·수동 발행용 원고
)


class ConfigError(ValueError):
    """환경변수가 누락되었거나 형식이 잘못된 경우."""


@dataclass(frozen=True)
class GoogleConfig:
    client_id: str
    client_secret: str
    refresh_token: str


@dataclass(frozen=True)
class Config:
    blog_name: str
    google: GoogleConfig | None
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_models: tuple[str, ...]
    slack_bot_token: str
    slack_channel_id: str
    slack_webhook_url: str
    blogger_blog_id: str
    topic_lookback_days: int
    min_references: int
    max_fix_rounds: int
    min_body_chars: int
    output_dir: Path
    slack_stage_channels: tuple[tuple[str, str], ...] = ()  # (진행 단계, 채널 ID)
    image_host_url: str = ""  # 이미지 저장소 (Cloudflare Worker 주소)
    image_upload_token: str = ""

    @property
    def image_host_enabled(self) -> bool:
        return bool(self.image_host_url and self.image_upload_token)

    @property
    def blogger_enabled(self) -> bool:
        return bool(self.blogger_blog_id)

    @property
    def slack_enabled(self) -> bool:
        return bool((self.slack_bot_token and self.slack_channel_id) or self.slack_webhook_url)

    def secret_values(self) -> tuple[str, ...]:
        """슬랙 전송 전 가려야 할 비밀값 목록."""
        google = (self.google.client_secret, self.google.refresh_token) if self.google else ()
        values = (
            *google,
            self.gemini_api_key,
            self.slack_bot_token,
            self.slack_webhook_url,
            self.image_upload_token,
        )
        return tuple(v for v in values if v)


def _get(env: Mapping[str, str], key: str, default: str = "") -> str:
    """공백만 있는 값도 '미설정'으로 보고 기본값을 쓴다."""
    return (env.get(key) or "").strip() or default


def _get_list(env: Mapping[str, str], key: str, default: str = "") -> tuple[str, ...]:
    """쉼표로 구분된 값을 목록으로 (빈 항목 제거, 순서·중복 정리)."""
    items = (item.strip() for item in _get(env, key, default).split(","))
    return tuple(dict.fromkeys(item for item in items if item))


def _get_int(env: Mapping[str, str], key: str, default: int, low: int, high: int) -> int:
    raw = _get(env, key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 값은 정수여야 합니다 (현재: {raw!r})") from exc
    if not low <= value <= high:
        raise ConfigError(f"{key} 값은 {low}~{high} 범위여야 합니다 (현재: {value})")
    return value


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """환경변수에서 설정을 읽는다. 형식 오류는 ConfigError로 즉시 실패한다."""
    source = os.environ if env is None else env

    client_id = _get(source, "GOOGLE_CLIENT_ID")
    client_secret = _get(source, "GOOGLE_CLIENT_SECRET")
    refresh_token = _get(source, "GOOGLE_REFRESH_TOKEN")
    google = (
        GoogleConfig(client_id, client_secret, refresh_token)
        if client_id and client_secret and refresh_token
        else None
    )

    return Config(
        blog_name=_get(source, "BLOG_NAME", DEFAULT_BLOG_NAME),
        google=google,
        gemini_api_key=_get(source, "GEMINI_API_KEY"),
        gemini_model=_get(source, "GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        gemini_fallback_models=_get_list(source, "GEMINI_FALLBACK_MODELS", DEFAULT_GEMINI_FALLBACKS),
        slack_bot_token=_get(source, "SLACK_BOT_TOKEN"),
        slack_channel_id=_get(source, "SLACK_CHANNEL_ID"),
        slack_webhook_url=_get(source, "SLACK_WEBHOOK_URL"),
        blogger_blog_id=_get(source, "BLOGGER_BLOG_ID"),
        topic_lookback_days=_get_int(source, "TOPIC_LOOKBACK_DAYS", 3, 1, 14),
        min_references=_get_int(source, "MIN_REFERENCES", 3, 1, 10),
        max_fix_rounds=_get_int(source, "MAX_FIX_ROUNDS", 2, 0, 4),
        min_body_chars=_get_int(source, "MIN_BODY_CHARS", 5000, 1000, 30000),
        output_dir=Path(_get(source, "OUTPUT_DIR", "output")),
        slack_stage_channels=tuple(
            (stage, _get(source, key)) for stage, key in STAGE_CHANNEL_ENV if _get(source, key)
        ),
        image_host_url=_get(source, "IMAGE_HOST_URL").rstrip("/"),
        image_upload_token=_get(source, "IMAGE_UPLOAD_TOKEN"),
    )


def validate_for_mode(config: Config, mode: str) -> list[str]:
    """실행 모드에 필요한 필수 설정 누락 목록을 반환한다 (빈 리스트면 정상)."""
    if mode not in MODES:
        return [f"알 수 없는 실행 모드: {mode!r} (post / setup 중 선택)"]

    errors: list[str] = []
    if config.google is None:
        errors.append(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN 이 필요합니다 "
            "(scripts/get_google_token.py 로 발급)"
        )
    if mode == "post":
        if not config.gemini_api_key:
            errors.append("GEMINI_API_KEY 가 필요합니다")
        if not config.blogger_enabled:
            errors.append("BLOGGER_BLOG_ID 가 필요합니다 (scripts/get_google_token.py 가 'AI백과사전' 블로그를 찾아 저장)")
    return errors
