import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_choose_blog_prefers_ai_encyclopedia(capsys):
    tokens = load("get_google_token")
    blogs = [{"id": "1", "name": "다른 블로그"}, {"id": "2", "name": "AI백과사전", "url": "https://ai.blogspot.com"}]
    assert tokens.choose_blog(blogs, "AI백과사전") == "2"
    assert tokens.choose_blog([{"id": "9", "name": "옛 이름"}], "AI백과사전") == "9"
    assert tokens.choose_blog([{"id": "1", "name": "a"}, {"id": "3", "name": "b"}], "AI백과사전") == ""
    assert tokens.choose_blog([], "AI백과사전") == ""
    assert "블로그를 만든 뒤" in capsys.readouterr().out
    assert tokens.DEFAULT_ACCOUNT == "rkdghkclgns@gmail.com"


def test_push_secrets_splits_secrets_and_variables():
    push = load("push_secrets")
    secrets, variables = push.split_values({"GEMINI_API_KEY": "k", "SLACK_BOT_TOKEN": " ", "BLOG_NAME": "AI백과사전",
                                            "UNKNOWN": "x", "AUTO_PUBLISH": "true"})
    assert secrets == {"GEMINI_API_KEY": "k"}
    assert variables == {"BLOG_NAME": "AI백과사전", "AUTO_PUBLISH": "true"}
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs["input"]))
        return subprocess.CompletedProcess(args, 1 if args[-1] == "AUTO_PUBLISH" else 0)

    assert push.upload_variables(variables, run) == ["AUTO_PUBLISH"]
    assert calls[0] == (["gh", "variable", "set", "BLOG_NAME"], "AI백과사전")


def test_slack_channels_are_created_once_and_user_invited():
    from slack_sdk.errors import SlackApiError

    channels = load("setup_slack_channels")

    class FakeSlack:
        def __init__(self):
            self.created, self.invited, self.joined = [], [], []
            self.existing = {"yoopost-알림": "CALERT"}

        def conversations_list(self, **kwargs):
            return {"channels": [{"name": n, "id": i} for n, i in self.existing.items()]}

        def conversations_create(self, name):
            self.created.append(name)
            return {"channel": {"id": f"C{len(self.created)}"}}

        def conversations_setPurpose(self, channel, purpose):
            pass

        def conversations_join(self, channel):
            self.joined.append(channel)

        def conversations_invite(self, channel, users):
            if channel == "C1":
                raise SlackApiError("already", {"error": "already_in_channel"})
            self.invited.append((channel, users))

    fake = FakeSlack()
    ids = channels.ensure_channels(fake, "U1")
    assert ids["yoopost-알림"] == "CALERT" and fake.joined == ["CALERT"]
    assert fake.created == ["yoopost-1-주제조사", "yoopost-2-원고검증", "yoopost-3-발행결과"]
    assert ("CALERT", "U1") in fake.invited and all(c != "C1" for c, _ in fake.invited)
    assert [env for _, env, secret in channels.CHANNELS if secret] == ["SLACK_CHANNEL_ID"]


def test_upload_variables_without_gh_reports_all_failed():
    from autopost.local_secrets import upload_variables

    def missing(*args, **kwargs):
        raise FileNotFoundError("gh")

    assert upload_variables({"A": "1", "B": "2"}, missing) == ["A", "B"]


def test_scripts_show_help_without_side_effects():
    for name in ("preview_post", "push_secrets", "get_google_token", "analyze_reference_blog", "setup_slack_channels",
                 "blogger_menu"):
        result = subprocess.run([sys.executable, str(SCRIPTS / f"{name}.py"), "--help"],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert result.returncode == 0, result.stderr
