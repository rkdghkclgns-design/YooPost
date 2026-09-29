import pytest

from autopost import cli
from autopost.config import ConfigError, load_config, validate_for_mode
from autopost.step_summary import build_failure_summary, build_step_summary, write_step_summary
from autopost.results import PublishResult

FULL = {
    "GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "sec", "GOOGLE_REFRESH_TOKEN": "tok",
    "GEMINI_API_KEY": "gem", "BLOGGER_BLOG_ID": "123", "SLACK_BOT_TOKEN": "xoxb-1", "SLACK_CHANNEL_ID": "C1",
    "SLACK_CHANNEL_RESEARCH": "CR", "SLACK_CHANNEL_PUBLISH": " ",
}


def test_defaults_and_stage_channels():
    cfg = load_config(FULL)
    assert cfg.blog_name == "AI백과사전" and not hasattr(cfg, "drive_root_name")  # 구글 드라이브는 쓰지 않음
    assert not hasattr(cfg, "deep_research_agent")  # 딥리서치(유료)는 쓰지 않음
    assert not cfg.image_host_enabled
    assert cfg.topic_lookback_days == 3 and cfg.min_references == 3
    assert cfg.slack_stage_channels == (("research", "CR"),)
    assert cfg.gemini_fallback_models[0] == "gemini-3.8-flash"
    assert cfg.blogger_enabled and cfg.slack_enabled
    assert not hasattr(cfg, "naver_blog_id")  # 네이버는 쓰지 않음
    assert set(cfg.secret_values()) >= {"sec", "tok", "gem", "xoxb-1"}


def test_image_host_settings():
    cfg = load_config({**FULL, "IMAGE_HOST_URL": "https://relay.example/", "IMAGE_UPLOAD_TOKEN": "img-tok"})
    assert cfg.image_host_enabled and cfg.image_host_url == "https://relay.example"
    assert "img-tok" in cfg.secret_values()


def test_invalid_values_fail_fast():
    with pytest.raises(ConfigError, match="정수"):
        load_config({"MIN_REFERENCES": "many"})
    with pytest.raises(ConfigError, match="범위"):
        load_config({"TOPIC_LOOKBACK_DAYS": "30"})
    with pytest.raises(ConfigError, match="MAX_FIX_ROUNDS"):
        load_config({"MAX_FIX_ROUNDS": "9"})


def test_validate_for_mode_and_readiness():
    assert validate_for_mode(load_config(FULL), "post") == []
    errors = validate_for_mode(load_config({}), "post")
    assert len(errors) == 3
    assert len(validate_for_mode(load_config({}), "setup")) == 1
    assert "알 수 없는" in validate_for_mode(load_config(FULL), "x")[0]
    assert "BLOGGER_BLOG_ID" in validate_for_mode(load_config({**FULL, "BLOGGER_BLOG_ID": ""}), "post")[0]


def test_parse_args_reads_actions_env(monkeypatch):
    monkeypatch.setenv("AUTOPOST_TOPIC", "Gemini 3.8")
    monkeypatch.setenv("AUTOPOST_DRY_RUN", "true")
    args = cli.parse_args([])
    assert args.topic == "Gemini 3.8" and args.dry_run and args.mode == "post"


def test_main_config_errors_return_2(monkeypatch):
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    for key in list(FULL) + ["MIN_REFERENCES", "BLOGGER_BLOG_ID"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MIN_REFERENCES", "x")
    assert cli.main([]) == 2
    monkeypatch.delenv("MIN_REFERENCES")
    assert cli.main(["--mode", "setup"]) == 2  # 구글 설정 없음


def test_main_runs_post_with_cleaned_topic(monkeypatch):
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    for key, value in FULL.items():
        monkeypatch.setenv(key, value)
    captured = {}
    monkeypatch.setattr(cli, "build_services", lambda cfg, slack, mode: "services")
    monkeypatch.setattr(cli, "run_post", lambda cfg, options, services: captured.setdefault("options", options) and 0)
    assert cli.main(["--topic", "  GPT-6   공개 ", "--dry-run"]) == 0
    assert captured["options"].topic == "GPT-6 공개" and captured["options"].dry_run


def test_main_reports_unexpected_errors(monkeypatch):
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    for key, value in FULL.items():
        monkeypatch.setenv(key, value)
    errors = []
    monkeypatch.setattr(cli.SlackNotifier, "notify_error", lambda self, stage, exc: errors.append(stage))

    def boom(*args):
        raise RuntimeError("blogger down")

    monkeypatch.setattr(cli, "build_services", boom)
    assert cli.main(["--mode", "setup"]) == 1 and errors == ["연동 점검"]
    monkeypatch.setattr(cli, "build_services", lambda *a: "s")
    monkeypatch.setattr(cli, "run_post", lambda *a: (_ for _ in ()).throw(cli.AlreadyReported("x")))
    assert cli.main([]) == 1 and errors == ["연동 점검"]  # 이미 보고된 오류는 다시 알리지 않음


def test_step_summary(tmp_path):
    text = build_step_summary("제목|1", "AI 도구", "https://src", 5000, "주장 3개 중 3개 확인",
                              [PublishResult("구글 블로그", True, "https://b"), PublishResult.skip("구글 블로그", "미설정")],
                              dry_run=False)
    assert "제목\\|1" in text and "[열기](https://b)" in text and "⏭️ 건너뜀" in text
    assert build_step_summary("t", "c", "", 1, "x", [], dry_run=True).startswith("## 🧪")
    assert "발행 보류" in build_step_summary("t", "c", "", 1, "x", [], dry_run=False, held=True)
    assert "```" not in build_failure_summary("AI", "원고", "a```b").split("```")[1]
    path = tmp_path / "s.md"
    assert write_step_summary("hello", {"GITHUB_STEP_SUMMARY": str(path)}) and path.read_text(encoding="utf-8") == "hello\n"
    assert not write_step_summary("x", {})
    assert not write_step_summary("x", {"GITHUB_STEP_SUMMARY": str(tmp_path / "no" / "dir" / "s.md")})


def test_build_services_wires_search_research_and_image_host(monkeypatch):
    monkeypatch.setattr(cli, "build_credentials", lambda google: "creds")
    monkeypatch.setattr(cli, "build_blogger_service", lambda creds: "blogger-service")
    monkeypatch.setattr(cli, "Gemini", lambda *a, **k: "gemini")
    cfg = load_config({**FULL, "IMAGE_HOST_URL": "https://relay.example", "IMAGE_UPLOAD_TOKEN": "t"})
    slack = cli.SlackNotifier()
    setup = cli.build_services(cfg, slack, "setup")
    assert setup.researcher is None and setup.blogger is not None and setup.image_host is not None
    post = cli.build_services(cfg, slack, "post")
    assert post.researcher is not None and not hasattr(post.researcher, "_deep")
    assert post.images is not None and post.image_host is not None
    no_host = cli.build_services(load_config(FULL), slack, "post")
    assert no_host.image_host is None
