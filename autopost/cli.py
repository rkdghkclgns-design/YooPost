"""명령행 진입점. main.py / setup.py 가 이 모듈을 호출한다.

인자가 없으면 환경변수(AUTOPOST_*)를 사용한다 — GitHub Actions 는 환경변수로 값을 넘긴다.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Sequence

from .blog_publisher import BloggerPublisher
from .config import MODES, Config, ConfigError, load_config, validate_for_mode
from .fact_check import FactChecker
from .gemini import Gemini
from .google_auth import build_blogger_service, build_credentials
from .image_host import ImageHost
from .links import USER_AGENT
from .pipeline import AlreadyReported, RunOptions, Services, run_post, run_setup
from .prune import Pruner
from .quote_images import ImageCollector
from .research import Researcher
from .slack_helper import SlackNotifier
from .topic import TopicSelector
from .writer import PostWriter

log = logging.getLogger("autopost")

TRUTHY = {"1", "true", "yes", "y", "on"}
MAX_TOPIC = 120


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUTHY


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI백과사전 자동 포스팅 (구글 블로그)")
    parser.add_argument("--mode", choices=MODES, default=os.environ.get("AUTOPOST_MODE") or "post")
    parser.add_argument("--topic", default=os.environ.get("AUTOPOST_TOPIC", ""),
                        help="쓸 주제 (예: 'Gemini 3.8 Live'). 비우면 최신 AI 소식에서 자동 선정")
    parser.add_argument("--dry-run", action="store_true", default=_env_flag("AUTOPOST_DRY_RUN"),
                        help="발행 없이 조사·원고·사실 검증·미리보기만 만들어 슬랙으로")
    parser.add_argument("--trigger", default=os.environ.get("AUTOPOST_TRIGGER", "manual"))
    parser.add_argument("--requested-by", default=os.environ.get("AUTOPOST_REQUESTED_BY", ""))
    return parser.parse_args(argv)


def http_session():
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.headers["Accept-Language"] = "ko-KR,ko;q=0.9,en;q=0.8"
    return session


def build_services(cfg: Config, slack: SlackNotifier, mode: str) -> Services:
    creds = build_credentials(cfg.google)
    session = http_session()
    blogger = BloggerPublisher(build_blogger_service(creds), cfg.blogger_blog_id) if cfg.blogger_enabled else None
    host = ImageHost(cfg.image_host_url, cfg.image_upload_token, session) if cfg.image_host_enabled else None
    if mode == "setup":
        return Services(slack=slack, blogger=blogger, image_host=host)

    gemini = Gemini(cfg.gemini_api_key, cfg.gemini_model, cfg.gemini_fallback_models)
    return Services(
        slack=slack,
        selector=TopicSelector(gemini, session, lookback_days=cfg.topic_lookback_days),
        researcher=Researcher(gemini, session, min_sources=cfg.min_references),
        writer=PostWriter(gemini, cfg.blog_name, cfg.min_body_chars),
        checker=FactChecker(gemini),
        pruner=Pruner(gemini),
        blogger=blogger,
        images=ImageCollector(session),
        image_host=host,
    )


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv()


def main(argv: Sequence[str] | None = None) -> int:
    _load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        stream=sys.stdout)
    args = parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("설정 오류: %s", exc)
        return 2

    slack = SlackNotifier(cfg.slack_bot_token, cfg.slack_channel_id, cfg.slack_webhook_url,
                          secrets=cfg.secret_values())
    errors = validate_for_mode(cfg, args.mode)
    if errors:
        for error in errors:
            log.error("설정 누락: %s", error)
        slack.notify_error("설정 검증", "\n".join(errors))
        return 2

    try:
        services = build_services(cfg, slack, args.mode)
        if args.mode == "setup":
            return run_setup(cfg, services)
        options = RunOptions(
            topic=" ".join(args.topic.split())[:MAX_TOPIC],
            dry_run=args.dry_run,
            trigger=args.trigger,
            requested_by=args.requested_by,
            run_url=os.environ.get("AUTOPOST_RUN_URL", ""),
        )
        return run_post(cfg, options, services)
    except AlreadyReported:
        log.exception("실행 중 오류 (슬랙 진행 스레드에 보고됨)")
        return 1
    except Exception as exc:
        log.exception("실행 중 오류")
        slack.notify_error("연동 점검" if args.mode == "setup" else "포스팅", exc)
        return 1
