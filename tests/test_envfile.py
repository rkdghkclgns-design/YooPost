import pytest

from autopost.envfile import update_env_file


def test_updates_existing_keys_and_keeps_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# 구글\nGOOGLE_CLIENT_ID=\nSLACK_CHANNEL_ID=C1\n# GOOGLE_REFRESH_TOKEN=old-comment\n", encoding="utf-8")
    update_env_file(env, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_REFRESH_TOKEN": "rt"})
    assert env.read_text(encoding="utf-8").splitlines() == [
        "# 구글",
        "GOOGLE_CLIENT_ID=cid",
        "SLACK_CHANNEL_ID=C1",
        "# GOOGLE_REFRESH_TOKEN=old-comment",  # 주석은 건드리지 않음
        "GOOGLE_REFRESH_TOKEN=rt",  # 없던 키는 끝에 추가
    ]


def test_creates_file_when_missing(tmp_path):
    env = update_env_file(tmp_path / ".env", {"A": "1"})
    assert env.read_text(encoding="utf-8") == "A=1\n"


def test_rejects_newlines(tmp_path):
    with pytest.raises(ValueError):
        update_env_file(tmp_path / ".env", {"A": "1\n2"})
