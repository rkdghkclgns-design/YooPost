import subprocess

from autopost import github_secrets

ENV = {"GH_SECRETS_TOKEN": "pat", "GITHUB_REPOSITORY": "me/repo"}


def test_returns_false_without_token_or_repo():
    assert github_secrets.update_github_secret("X", "v", env={}) is False
    assert github_secrets.update_github_secret("X", "v", env={"GH_SECRETS_TOKEN": "pat"}) is False


def test_passes_value_via_stdin_not_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(github_secrets.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(github_secrets.subprocess, "run",
                        lambda args, **kw: calls.append((args, kw)) or subprocess.CompletedProcess(args, 0))
    assert github_secrets.update_github_secret("TIKTOK_REFRESH_TOKEN", "secret-value", env=ENV) is True
    args, kwargs = calls[0]
    assert args == ["/usr/bin/gh", "secret", "set", "TIKTOK_REFRESH_TOKEN", "--repo", "me/repo"]
    assert "secret-value" not in args
    assert kwargs["input"] == "secret-value"
    assert kwargs["env"]["GH_TOKEN"] == "pat"


def test_failure_exit_code_returns_false(monkeypatch):
    monkeypatch.setattr(github_secrets.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(github_secrets.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1))
    assert github_secrets.update_github_secret("X", "v", env=ENV) is False


def test_os_error_returns_false(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no gh")

    monkeypatch.setattr(github_secrets.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(github_secrets.subprocess, "run", boom)
    assert github_secrets.update_github_secret("X", "v", env=ENV) is False
