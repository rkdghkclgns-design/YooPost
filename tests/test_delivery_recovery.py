"""요청 재전송·외부 발행 응답 유실·저장 실패를 주입해 중복 실행을 검사한다."""
from copy import deepcopy

import pytest
import requests

from autopost.cloud_drafts import write_json
from autopost.draft_request import request_path
from autopost.history import HistoryEntry, history_from_posts, read_marker
from autopost.manual_request import FireResult, handle_request
from autopost.publish_draft import publication_html, publish_draft
from autopost.slack_helper import SlackNotifier
from fakes import FakeBlogger, FakeWebClient
from test_cloud_drafts import load_script

RID = "C0MANUAL01-1791234000123456"
DELIVERY = "d" + "a" * 64
HOST = "https://yoopost-slack-relay.cleaning-autopost-slack-relay.workers.dev"


class Store:
    def __init__(self):
        self.value = {"status": "approved", "approved_by": "auto-60s", "title": "새 소식",
                      "html": "<p>본문</p>", "slack": {"channel": "CDRAFT", "ts": "1700.1"},
                      "request": {"id": RID}}
        self.fail_published = False
        self.fail_all = False

    def get(self, _):
        return deepcopy(self.value)

    def put(self, _, value):
        if self.fail_all or (self.fail_published and value.get("status") == "published"):
            raise TimeoutError("state save failed")
        self.value = deepcopy(value)


def test_final_save_failure_recovers_existing_post_and_all_result_channels():
    store, blogger, client = Store(), FakeBlogger(), FakeWebClient()
    store.fail_published = True
    slack = SlackNotifier(channel_id="CDRAFT", client=client)
    with pytest.raises(TimeoutError):
        publish_draft("d1", store, blogger, slack)
    assert len(blogger.posts) == 1 and store.value["status"] == "approved"
    store.fail_published = False
    assert publish_draft("d1", store, blogger, slack, "U1", "CPUB", manual_channel="C0MANUAL01") == 0
    assert len(blogger.posts) == 1 and store.value["status"] == "published"
    assert store.value["approved_by"] == "auto-60s" and not store.value["publish_unknown"]
    assert {m["channel"] for m in client.messages} == {"CDRAFT", "CPUB", "C0MANUAL01"}
    assert all("🤖✅" in m["text"] for m in client.messages)


def test_insert_accepted_but_response_lost_is_not_inserted_again():
    class LostResponse(FakeBlogger):
        def publish(self, *args):
            super().publish(*args)
            raise TimeoutError("insert response lost")
    store, blogger = Store(), LostResponse()
    slack = SlackNotifier(channel_id="CDRAFT", client=FakeWebClient())
    assert publish_draft("d1", store, blogger, slack) == 1
    assert store.value["publish_unknown"] and len(blogger.posts) == 1
    assert publish_draft("d1", store, blogger, slack) == 0
    assert len(blogger.posts) == 1 and store.value["status"] == "published"


def test_failure_before_insert_never_writes_to_blogger():
    store, blogger = Store(), FakeBlogger()
    store.fail_all = True
    with pytest.raises(TimeoutError):
        publish_draft("d1", store, blogger, SlackNotifier(client=FakeWebClient()))
    assert not blogger.posts


def test_history_read_failure_does_not_blindly_retry_unknown_publication():
    store, blogger = Store(), FakeBlogger(list_fail=True)
    store.value["publish_unknown"] = True
    assert publish_draft("d1", store, blogger, SlackNotifier(client=FakeWebClient())) == 1
    assert not blogger.posts and store.value["status"] == "failed"


@pytest.mark.parametrize("status", ["pending", "held", "excluded", "published"])
def test_non_approved_status_never_inserts(status):
    store, blogger = Store(), FakeBlogger()
    store.value["status"] = status
    publish_draft("d1", store, blogger, SlackNotifier(client=FakeWebClient()), "auto-60s")
    assert not blogger.posts


def test_marker_preserves_metadata_and_history_round_trip():
    draft = {"html": '<p>본문</p><!--yoopost {"topic":"topic", "source":"https://s/x", "sources":["https://s/x"]}-->'}
    html = publication_html("d1", draft)
    assert html.count("<!--yoopost") == 1
    assert read_marker(html)["sources"] == ["https://s/x"]
    entries = history_from_posts([{"title": "t", "url": "https://b/p/1", "content": html}]).entries
    assert entries[0].draft_id == "d1"
    assert HistoryEntry.from_dict(entries[0].to_dict()).draft_id == "d1"


def test_same_delivery_or_message_finds_record_before_refiring(tmp_path):
    helper = load_script("manual_request")
    record = {"id": RID, "delivery_id": DELIVERY}
    write_json(request_path(tmp_path, RID), record)
    assert helper.existing_request(tmp_path, DELIVERY, "", "") == record  # slash 재전송
    assert helper.existing_request(tmp_path, "", "C0MANUAL01", "1791234000.123456") == record
    assert not helper.existing_request(tmp_path, "d" + "b" * 64, "", "")


def test_delivery_ack_only_uses_pinned_worker_and_handles_network_failure(monkeypatch):
    helper = load_script("manual_request")
    calls = []
    monkeypatch.setenv("IMAGE_HOST_URL", HOST)
    monkeypatch.setenv("IMAGE_UPLOAD_TOKEN", "test")
    def post(url, **kwargs):
        calls.append(url)
        return type("Response", (), {"ok": True})()
    monkeypatch.setattr(requests, "post", post)
    assert helper.acknowledge_delivery(DELIVERY)
    assert calls == [HOST + "/delivery/" + DELIVERY]
    assert not helper.acknowledge_delivery("../secret")
    monkeypatch.setenv("IMAGE_HOST_URL", "https://evil.example")
    assert not helper.acknowledge_delivery(DELIVERY) and len(calls) == 1
    monkeypatch.setenv("IMAGE_HOST_URL", HOST)
    def fail(*a, **kw): raise requests.Timeout()
    monkeypatch.setattr(requests, "post", fail)
    assert not helper.acknowledge_delivery(DELIVERY)


@pytest.mark.parametrize("error", ["루틴 토큰이 맞지 않습니다", "HTTP 400", "HTTP 403", "Claude 루틴 설정 누락"])
def test_definite_claude_failure_uses_codex_after_recording(error):
    saved, dispatches = [], []
    def fallback(rid):
        assert saved[-1]["fire_error"] == error
        dispatches.append(rid)
        return True
    status = handle_request("AI 도구 한 편 더 발행해줘", "C0MANUAL01", "1791234000.123456", "U1", "message",
        manual_channel="C0MANUAL01", notifier=SlackNotifier(client=FakeWebClient()),
        fire=None if error == "Claude 루틴 설정 누락" else lambda _: FireResult(False, error=error),
        record=saved.append, fallback=fallback, delivery_id=DELIVERY)
    assert status == "fallback" and dispatches == [RID] and saved[0]["delivery_id"] == DELIVERY


def test_uncertain_claude_timeout_waits_for_watchdog_instead_of_refiring():
    watched, dispatched = [], []
    status = handle_request("AI 도구 한 편 더 발행해줘", "C0MANUAL01", "1791234000.123456", "U1", "message",
        manual_channel="C0MANUAL01", notifier=SlackNotifier(client=FakeWebClient()),
        fire=lambda _: FireResult(False, error="응답 없음(Timeout)"), record=lambda r: None,
        fallback=lambda rid: dispatched.append(rid), watch=lambda *a: watched.append(a) or True)
    assert status == "failed" and watched and not dispatched


def test_delivery_retry_main_acknowledges_existing_request_without_refiring(tmp_path, monkeypatch):
    helper = load_script("manual_request")
    write_json(request_path(tmp_path, RID), {"id": RID, "delivery_id": DELIVERY,
        "created": "2026-10-06T13:00:00+09:00", "requested_by": "U1"})
    for key, value in {"SLACK_CHANNEL_MANUAL": "C0MANUAL01", "SLACK_BOT_TOKEN": "test", "MANUAL_DELIVERY_ID": DELIVERY,
                       "MANUAL_CHANNEL": "", "MANUAL_TS": ""}.items(): monkeypatch.setenv(key, value)
    acked = []
    monkeypatch.setattr(helper, "acknowledge_delivery", lambda d: acked.append(d))
    monkeypatch.setattr(helper, "arm_request_watch", lambda *a: True)
    monkeypatch.setattr(helper, "handle_request", lambda *a, **k: pytest.fail("must not refire"))
    assert helper.main(["--tree", str(tmp_path)]) == 0 and acked == [DELIVERY]
