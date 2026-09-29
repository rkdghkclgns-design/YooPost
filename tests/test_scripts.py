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


def test_scripts_show_help_without_side_effects():
    for name in ("preview_post", "push_secrets", "get_google_token", "analyze_reference_blog"):
        result = subprocess.run([sys.executable, str(SCRIPTS / f"{name}.py"), "--help"],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert result.returncode == 0, result.stderr
