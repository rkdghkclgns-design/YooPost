import base64
import json
from datetime import date, datetime, timezone

from autopost import token_expiry
from autopost.constants import GUIDE_FILE, HISTORY_FILE, PUBLIC_ASSET_FOLDER
from autopost.setup_engine import ensure_layout
from autopost.token_expiry import build_message, collect, naver_expiry, needs_alert
from fakes import MemoryDrive


def test_setup_creates_layout_once():
    drive = MemoryDrive()
    first = ensure_layout(drive, "AI백과사전_포스팅자동화")
    assert first.changed
    assert first.created_folders == ("AI백과사전_포스팅자동화", f"AI백과사전_포스팅자동화/{PUBLIC_ASSET_FOLDER}")
    assert set(first.created_files) == {GUIDE_FILE, HISTORY_FILE}
    second = ensure_layout(drive, "AI백과사전_포스팅자동화")
    assert not second.changed and second.layout == first.layout
    history = drive.find_child(first.layout.root_id, HISTORY_FILE)
    assert json.loads(drive.read_text(history.id)) == {"posts": []}


def _state(expires) -> str:
    cookies = [{"name": "NID_AUT", "expires": expires}] if expires is not None else []
    return base64.b64encode(json.dumps({"cookies": cookies}).encode()).decode()


def test_naver_expiry_date_and_unknowns():
    ts = datetime(2026, 10, 2, 3, tzinfo=timezone.utc).timestamp()
    assert naver_expiry(_state(ts)).expires == date(2026, 10, 2)
    assert naver_expiry(_state(-1)).expires is None
    assert naver_expiry(_state(None)).expires is None
    assert naver_expiry("garbage").expires is None


def test_alert_message_within_three_days():
    today = date(2026, 9, 29)
    soon = token_expiry.TokenExpiry("네이버", date(2026, 10, 2))
    later = token_expiry.TokenExpiry("네이버", date(2026, 12, 1))
    assert needs_alert([soon], today) and not needs_alert([later], today)
    message = build_message([soon], today)
    assert "네이버 3일 이내 만료" in message and "• 네이버: 2026-10-02 (3일 남음)" in message
    assert "export_naver_cookies.py" in message
    assert "확인용" in build_message([later], today)
    assert "확인 불가" in token_expiry.TokenExpiry("네이버", None).line(today)
    assert "만료됨" in token_expiry.TokenExpiry("네이버", date(2026, 9, 1)).line(today)
    assert "오늘 만료" in token_expiry.TokenExpiry("네이버", today).line(today)
    assert collect({}) == []


def test_main_posts_only_when_needed(monkeypatch, capsys):
    posted = []
    monkeypatch.setattr(token_expiry, "_post", lambda text, env: posted.append(text) or True)
    ts = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
    env = {"NAVER_STORAGE_STATE_B64": _state(ts)}
    assert token_expiry.main([], env, date(2026, 9, 29)) == 0 and len(posted) == 1
    assert token_expiry.main(["--dry-run"], env, date(2026, 9, 29)) == 0 and len(posted) == 1
    far = {"NAVER_STORAGE_STATE_B64": _state(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())}
    assert token_expiry.main([], far, date(2026, 9, 29)) == 0 and len(posted) == 1
    assert token_expiry.main(["--force"], far, date(2026, 9, 29)) == 0 and len(posted) == 2
    assert token_expiry.main([], {}, date(2026, 9, 29)) == 0
    assert "점검할 토큰이 없습니다" in capsys.readouterr().out


def test_post_requires_token_and_channel():
    assert token_expiry._post("x", {}) is False
