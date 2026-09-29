import pytest

from autopost.local_secrets import save_everywhere, upload_secrets
from autopost.setup_browser import launch_visible_browser


def _runner(calls, fail=()):
    def run(cmd, input, text, capture_output):
        calls.append((cmd, input))
        return type("R", (), {"returncode": 1 if cmd[-1] in fail else 0})()

    return run


def test_upload_secrets_sends_values_via_stdin_only():
    calls = []
    failed = upload_secrets({"A": "secret-a", "B": "secret-b"}, run=_runner(calls, fail=("B",)))
    assert failed == ["B"]
    assert calls[0] == (["gh", "secret", "set", "A"], "secret-a")
    assert all("secret-" not in " ".join(cmd) for cmd, _ in calls)  # 값은 명령줄 인자에 넣지 않음


def test_upload_secrets_without_gh_cli_reports_all_failed():
    def missing(*args, **kwargs):
        raise FileNotFoundError("gh")

    assert upload_secrets({"A": "1", "B": "2"}, run=missing) == ["A", "B"]


def test_save_everywhere_updates_env_and_optionally_github(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# 설정\nNAVER_BLOG_ID=\nOTHER=keep\n", encoding="utf-8")
    calls = []
    assert save_everywhere({"NAVER_BLOG_ID": "evbank"}, env, run=_runner(calls)) == []
    assert env.read_text(encoding="utf-8") == "# 설정\nNAVER_BLOG_ID=evbank\nOTHER=keep\n"
    assert calls == [(["gh", "secret", "set", "NAVER_BLOG_ID"], "evbank")]

    calls.clear()
    save_everywhere({"X": "1"}, env, upload=False, run=_runner(calls))
    assert calls == [] and "X=1" in env.read_text(encoding="utf-8")


def test_launch_visible_browser_falls_back_through_channels():
    tried = []

    class Chromium:
        def launch(self, headless, channel=None):
            tried.append(channel)
            if channel != "msedge":
                raise RuntimeError("spawn UNKNOWN")
            return "edge-browser"

    assert launch_visible_browser(Chromium()) == "edge-browser"
    assert tried == ["chrome", "msedge"]


def test_launch_visible_browser_reports_last_error():
    class Broken:
        def launch(self, headless, channel=None):
            raise RuntimeError(f"no {channel}")

    with pytest.raises(RuntimeError, match="no None"):
        launch_visible_browser(Broken())
