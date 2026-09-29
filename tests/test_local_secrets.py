from autopost.local_secrets import save_everywhere, upload_secrets


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
    env.write_text("# 설정\nBLOGGER_BLOG_ID=\nOTHER=keep\n", encoding="utf-8")
    calls = []
    assert save_everywhere({"BLOGGER_BLOG_ID": "7990695773373420731"}, env, run=_runner(calls)) == []
    assert env.read_text(encoding="utf-8") == "# 설정\nBLOGGER_BLOG_ID=7990695773373420731\nOTHER=keep\n"
    assert calls == [(["gh", "secret", "set", "BLOGGER_BLOG_ID"], "7990695773373420731")]

    calls.clear()
    save_everywhere({"X": "1"}, env, upload=False, run=_runner(calls))
    assert calls == [] and "X=1" in env.read_text(encoding="utf-8")

