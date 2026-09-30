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


def test_slack_channels_follow_the_approval_workflow():
    from slack_sdk.errors import SlackApiError

    channels = load("setup_slack_channels")

    class FakeSlack:
        def __init__(self, existing):
            self.existing = existing  # 이름 → {"id", "is_archived"}
            self.created, self.renamed, self.archived, self.invited, self.purposes, self.posts = [], [], [], [], {}, []

        def conversations_list(self, **kwargs):
            return {"channels": [{"name": n, **c} for n, c in self.existing.items()]}

        def conversations_create(self, name):
            self.created.append(name)
            return {"channel": {"id": f"C{len(self.created)}"}}

        def conversations_rename(self, channel, name):
            self.renamed.append((channel, name))

        def conversations_archive(self, channel):
            self.archived.append(channel)

        def conversations_setPurpose(self, channel, purpose):
            self.purposes[channel] = purpose

        def conversations_join(self, channel):
            pass

        def conversations_invite(self, channel, users):
            if channel == "CPUB":
                raise SlackApiError("already", {"error": "already_in_channel"})
            self.invited.append((channel, users))

        def chat_postMessage(self, channel, text):
            self.posts.append((channel, text))

    old = FakeSlack({"yoopost-알림": {"id": "CALERT"}, "yoopost-1-주제조사": {"id": "CRES"},
                     "yoopost-2-원고검증": {"id": "CDRAFT"}, "yoopost-3-발행결과": {"id": "CPUB"},
                     "yoopost-예전": {"id": "COLD", "is_archived": True}})
    known = channels.all_channels(old)
    assert channels.ensure_channel(old, known, "yoopost-1-원고승인", ("yoopost-2-원고검증",), "p") == ("CDRAFT", True)
    assert channels.ensure_channel(old, known, "yoopost-알림", (), "q") == ("CALERT", False)
    channels.retire(old, known)
    assert old.renamed == [("CDRAFT", "yoopost-1-원고승인")] and old.archived == ["CRES"] and old.created == []
    assert old.purposes == {"CDRAFT": "p", "CALERT": "q"}

    fresh = FakeSlack({})
    assert channels.ensure_channel(fresh, {}, "yoopost-2-발행완료", ("yoopost-3-발행결과",), "r") == ("C1", True)
    channels.retire(fresh, {"yoopost-1-주제조사": {"id": "X", "is_archived": True}})
    assert fresh.created == ["yoopost-2-발행완료"] and fresh.archived == []
    channels.invite(fresh, "C1", "U1", "n")
    channels.invite(fresh, "CPUB", "U1", "n")
    channels.invite(fresh, "C1", None, "n")
    assert fresh.invited == [("C1", "U1")]
    assert [env for _, _, env, secret, _ in channels.CHANNELS if secret] == ["SLACK_CHANNEL_ID"]
    assert [name for name, *_ in channels.CHANNELS] == [
        "yoopost-0-수동지시", "yoopost-알림", "yoopost-1-원고승인", "yoopost-2-발행완료"]
    assert set(channels.GUIDES) == {"SLACK_CHANNEL_DRAFT", "SLACK_CHANNEL_MANUAL"}


def test_upload_variables_without_gh_reports_all_failed():
    from autopost.local_secrets import upload_variables

    def missing(*args, **kwargs):
        raise FileNotFoundError("gh")

    assert upload_variables({"A": "1", "B": "2"}, missing) == ["A", "B"]


def test_scripts_show_help_without_side_effects():
    for name in ("preview_post", "push_secrets", "get_google_token", "analyze_reference_blog", "setup_slack_channels",
                 "blogger_menu", "slack_notice", "claude_brief", "claude_draft", "cloud_drafts", "cloud_sync", "restyle_posts",
                 "draft_progress", "manual_request", "add_images"):
        result = subprocess.run([sys.executable, str(SCRIPTS / f"{name}.py"), "--help"],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert result.returncode == 0, result.stderr
