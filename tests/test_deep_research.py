import pytest

from autopost.deep_research import DeepResearch, DeepResearchError, cited_urls, report_text
from fakes import FakeDeepClient, FakeInteraction, deep_output


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self):
        return self.now


def deep(responses, timeout_minutes=45, poll=60):
    clock = FakeClock()
    client = FakeDeepClient(responses)
    runner = DeepResearch("key", "deep-research-preview-04-2026", client=client, timeout_minutes=timeout_minutes,
                          poll_seconds=poll, sleep=clock.sleep, clock=clock)
    return runner, client, clock


def test_runs_in_background_polls_and_returns_report():
    done = FakeInteraction("completed", deep_output("보고서 [cite: S1]", ["https://a.com/x", "https://a.com/x/"]))
    runner, client, clock = deep([FakeInteraction("in_progress"), FakeInteraction("in_progress"),
                                  FakeInteraction("in_progress"), done])
    notes = []
    report = runner.run("조사해 주세요", progress=notes.append)
    assert report.text == "보고서 [cite: S1]" and report.cited_urls == ("https://a.com/x",)
    assert report.interaction_id == "int-1" and report.minutes == 3.0
    created = client.interactions.created[0]
    assert created["background"] is True and created["agent"] == "deep-research-preview-04-2026"
    assert created["agent_config"]["type"] == "deep-research" and created["agent_config"]["visualization"] == "off"
    assert notes == ["구글 딥리서치 조사 중 — 2분 경과"]  # 2분마다 진행 상황
    assert runner.agent == "deep-research-preview-04-2026"


def test_timeout_cancels_and_raises():
    runner, client, _ = deep([FakeInteraction("in_progress")] * 5, timeout_minutes=2, poll=60)
    with pytest.raises(DeepResearchError, match="2분 안에"):
        runner.run("x")
    assert client.interactions.cancelled == ["int-1"]


def test_failed_status_and_empty_report_raise():
    runner, _, _ = deep([FakeInteraction("in_progress"), FakeInteraction("failed", error="quota exceeded")])
    with pytest.raises(DeepResearchError, match="quota exceeded"):
        runner.run("x")
    runner, _, _ = deep([FakeInteraction("completed", deep_output(""))])
    with pytest.raises(DeepResearchError, match="비어"):
        runner.run("x")


def test_transient_poll_errors_are_retried_until_limit():
    done = FakeInteraction("completed", deep_output("ok"))
    runner, _, _ = deep([FakeInteraction("in_progress"), RuntimeError("503"), RuntimeError("reset"), done])
    assert runner.run("x").text == "ok"
    runner, _, _ = deep([FakeInteraction("in_progress")] + [RuntimeError("down")] * 6)
    with pytest.raises(DeepResearchError, match="상태를 확인할 수 없습니다"):
        runner.run("x")


def test_cancel_failures_are_ignored():
    class NoCancel:
        def __init__(self):
            self.interactions = type("I", (), {
                "create": lambda self, **k: FakeInteraction("in_progress"),
                "get": lambda self, i: FakeInteraction("in_progress"),
                "cancel": lambda self, i: (_ for _ in ()).throw(RuntimeError("nope")),
            })()

    clock = FakeClock()
    runner = DeepResearch("k", "a", client=NoCancel(), timeout_minutes=1, poll_seconds=60, sleep=clock.sleep,
                          clock=clock)
    with pytest.raises(DeepResearchError):
        runner.run("x")


def test_report_text_and_citations_handle_other_shapes():
    legacy = {"outputs": [{"type": "text", "text": "A", "annotations": [{"url": "https://x.com/1"}]},
                          {"type": "image", "data": "..."}]}
    assert report_text(legacy) == "A" and cited_urls(legacy) == ("https://x.com/1",)
    assert report_text({"steps": [], "output_text": "B"}) == "B"
    # 본문 글자 속 주소는 인용으로 치지 않는다
    assert cited_urls(deep_output("see https://y.com")) == ()
