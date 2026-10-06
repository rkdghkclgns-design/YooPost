import json
from datetime import datetime, timedelta, timezone

import pytest

from autopost.manual_request import FireResult, fire_routine, handle_request, valid_fire_url
from autopost.slack_helper import SlackNotifier
from fakes import FakeWebClient

MANUAL = "C0MANUAL01"
URL = "https://api.anthropic.com/v1/claude_code/routines/trig_01ABCDEFGHJKLMNOPQRSTUVW/fire"
NOW = datetime(2026, 9, 30, 0, 10, tzinfo=timezone.utc)


class FakeFire:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or FireResult(True, "https://claude.ai/code/session_01X")

    def __call__(self, text):
        self.calls.append(json.loads(text))
        return self.result


RECORDS = []


def run(text, source="message", channel=MANUAL, ts="1727650000.123456", fire=None, configured=True, record=None):
    client = FakeWebClient()
    notifier = SlackNotifier(client=client, channel_id=MANUAL)
    fire = fire or FakeFire()
    RECORDS.clear()
    status = handle_request(text, channel, ts, "U0C4PLGBNF9", source, manual_channel=MANUAL,
                            notifier=notifier, fire=fire if configured else None,
                            record=record or RECORDS.append, now=NOW)
    return status, client, fire


def test_single_request_fires_the_routine_and_replies_in_thread():
    status, client, fire = run("슬랙에 에이전트 직원 만들기를 AI도구에 발행해줘")
    assert status == "fired"
    assert fire.calls == [{"yoopost_manual_request": 1, "id": "C0MANUAL01-1727650000123456", "kind": "single",
                           "category": "AI 도구"}]
    assert RECORDS == [{  # 루틴을 깨우기 전에 요청 기록(주제 포함)을 원고 브랜치에 남긴다
        "id": "C0MANUAL01-1727650000123456", "kind": "single", "category": "AI 도구",
        "topic": "슬랙에 에이전트 직원 만들기", "requested_by": "U0C4PLGBNF9", "created": "2026-09-30T09:10:00+09:00",
        "started": "", "network": "", "finished": "", "note": "",
    }]
    reply = client.messages[-1]
    assert reply["channel"] == MANUAL and reply["thread_ts"] == "1727650000.123456"
    assert "AI 도구" in reply["text"] and "슬랙에 에이전트 직원 만들기" in reply["text"]
    assert "claude.ai/code/session_01X" in reply["text"] and "✅" in reply["text"]


def test_all_request():
    status, client, fire = run("전체 새로운 포스트로 재발행")
    assert status == "fired" and fire.calls[0]["kind"] == "all"
    assert "7개 카테고리" in client.messages[-1]["text"]


def test_help_and_invalid_do_not_fire():
    for text, expected in (("도움말", "help"), ("발행해줘", "invalid")):
        status, client, fire = run(text)
        assert status == expected and fire.calls == []
        assert "사용법" in client.messages[-1]["text"]


def test_messages_from_other_channels_are_ignored():
    status, client, fire = run("AI 보안 한 편 더", channel="C0OTHER999")
    assert status == "ignored" and fire.calls == [] and client.messages == []


def test_slash_command_opens_a_thread_in_the_manual_channel():
    status, client, fire = run("AI 보안 한 편 더 발행해줘", source="slash", channel="C0OTHER999", ts="")
    assert status == "fired"
    root, reply = client.messages
    assert root["channel"] == MANUAL and root.get("thread_ts") is None
    assert "/aipost" in root["text"] and "AI 보안 한 편 더 발행해줘" in root["text"]
    assert reply["thread_ts"] == "1700000000.000001"
    assert fire.calls[0]["id"] == "C0MANUAL01-1700000000000001"


def test_missing_setup_and_fire_errors_are_reported_in_thread():
    status, client, _ = run("AI 보안 한 편 더 발행해줘", configured=False)
    assert status == "not configured" and "ROUTINE_FIRE_TOKEN" in client.messages[-1]["text"]
    status, client, _ = run("AI 보안 한 편 더 발행해줘", fire=FakeFire(FireResult(False, error="한 시간 실행 한도 (3600초 뒤 다시)")))
    assert status == "failed" and "한 시간 실행 한도" in client.messages[-1]["text"]


def test_fire_failure_closes_the_recorded_request():
    status, client, _ = run("AI 보안 한 편 더 발행해줘", fire=FakeFire(FireResult(False, error="HTTP 429")))
    assert status == "failed"
    assert [r.get("fire_error", "") for r in RECORDS] == ["", "HTTP 429"]  # 기록 → 실행 실패 표시
    assert "다시 시도" in client.messages[-1]["text"]


def test_chit_chat_gets_a_short_hint_not_the_whole_manual():
    status, client, fire = run("고마워")
    assert status == "invalid" and fire.calls == [] and RECORDS == []
    assert "발행해줘" in client.messages[-1]["text"] and "사용법" not in client.messages[-1]["text"]


def test_record_failure_stops_before_firing():
    def broken(record):
        raise RuntimeError("git push failed")

    status, client, fire = run("AI 보안 한 편 더 발행해줘", record=broken)
    assert status == "failed" and fire.calls == []
    assert "기록하지 못해" in client.messages[-1]["text"]


def test_user_text_is_escaped_in_slack_replies():
    _, client, _ = run("<!channel> 공지 <b>를 AI 도구에 발행해줘", source="slash", channel="C0OTHER999", ts="")
    assert "<!channel>" not in client.messages[0]["text"]


def test_valid_fire_url_only_allows_the_routine_endpoint():
    assert valid_fire_url(URL)
    assert not valid_fire_url("https://evil.example/v1/claude_code/routines/trig_01A/fire")
    assert not valid_fire_url("http://api.anthropic.com/v1/claude_code/routines/trig_01A/fire")
    assert not valid_fire_url(URL + "?x=1")


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self):
        return self._body


def test_fire_routine_success_and_errors():
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    responses = [FakeResponse(200, {"claude_code_session_url": "https://claude.ai/code/session_01Y"})]
    result = fire_routine(URL, "sk-ant-oat01-secret", '{"a":1}', post=post)
    assert result == FireResult(True, "https://claude.ai/code/session_01Y")
    url, kwargs = calls[0]
    assert url == URL and kwargs["json"] == {"text": '{"a":1}'}
    assert kwargs["headers"]["Authorization"] == "Bearer sk-ant-oat01-secret"
    assert kwargs["headers"]["anthropic-version"] == "2023-06-01"

    responses = [FakeResponse(429, headers={"Retry-After": "1200"})]
    assert "1200" in fire_routine(URL, "t", "x", post=post).error
    responses = [FakeResponse(400, {"error": {"message": "routine is paused"}})]
    assert "paused" in fire_routine(URL, "t", "x", post=post).error
    responses = [FakeResponse(401)]
    assert "토큰" in fire_routine(URL, "t", "x", post=post).error
    responses = [FakeResponse(503), FakeResponse(200, {"claude_code_session_url": "u"})]
    assert fire_routine(URL, "t", "x", post=post, wait=lambda s: None).ok  # 503 은 세션이 안 만들어졌으니 재시도
    assert not fire_routine("https://evil.example/fire", "t", "x", post=post).ok


def test_fire_routine_does_not_retry_timeouts():
    attempts = []

    def post(url, **kwargs):
        attempts.append(1)
        raise TimeoutError("read timeout")

    result = fire_routine(URL, "t", "x", post=post, wait=lambda s: None)
    assert not result.ok and len(attempts) == 1  # 세션이 만들어졌을 수 있으니 중복 실행을 피한다


def test_accepted_request_arms_watchdog_from_record_creation_time():
    client = FakeWebClient()
    notifier = SlackNotifier(client=client, channel_id=MANUAL)
    recorded, watched = [], []
    status = handle_request("전체 새로운 포스트로 재발행", MANUAL, "1727650000.123456", "U1", "message",
        manual_channel=MANUAL, notifier=notifier, fire=FakeFire(), record=recorded.append, now=NOW,
        watch=lambda *args: watched.append(args) or True)
    assert status == "fired"
    assert watched == [(recorded[0]["id"], recorded[0]["created"], "U1")]
    assert "15분" in client.messages[-1]["text"] and "Codex" in client.messages[-1]["text"]


def test_watchdog_error_keeps_the_accepted_request_and_does_not_refire_claude():
    client, fire = FakeWebClient(), FakeFire()
    def broken(*args):
        raise TimeoutError("watchdog unavailable")
    status = handle_request("전체 새로운 포스트로 재발행", MANUAL, "1727650000.123456", "U1", "message",
        manual_channel=MANUAL, notifier=SlackNotifier(client=client, channel_id=MANUAL),
        fire=fire, record=lambda r: None, now=NOW, watch=broken)
    assert status == "fired" and len(fire.calls) == 1
    assert "접수했습니다" in client.messages[-1]["text"]
    assert "15분" not in client.messages[-1]["text"]


def test_watch_registration_only_sends_secret_to_the_configured_worker(monkeypatch):
    import requests
    from test_cloud_drafts import load_script
    helper = load_script("manual_request")
    host = "https://yoopost-slack-relay.cleaning-autopost-slack-relay.workers.dev"
    monkeypatch.setenv("IMAGE_HOST_URL", host)
    monkeypatch.setenv("IMAGE_UPLOAD_TOKEN", "secret")
    monkeypatch.setenv("CODEX_FALLBACK_ENABLED", "true")
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return type("Response", (), {"ok": True, "json": lambda self: {"ok": True}})()
    monkeypatch.setattr(requests, "post", post)
    assert helper.arm_request_watch("C0MANUAL01-1727650000123456", NOW.isoformat(), "U1")
    assert calls[0][0] == host + "/manual-watch/C0MANUAL01-1727650000123456"
    assert calls[0][1]["json"] == {"created": NOW.isoformat(), "user": "U1"}
    monkeypatch.setenv("IMAGE_HOST_URL", "https://evil.example")
    assert not helper.arm_request_watch("r", NOW.isoformat(), "U1")
    assert len(calls) == 1
