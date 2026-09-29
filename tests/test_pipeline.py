"""전체 흐름 — 외부 서비스는 가짜로 두고 단계 순서·발행 보류·기록·슬랙 보고를 확인한다."""
import json
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from autopost.config import load_config
from autopost.constants import HISTORY_FILE
from autopost.content import parse_post
from autopost.fact_check import ClaimVerdict, FactCheckReport
from autopost.history import HistoryEntry, PostHistory
from autopost.pipeline import AlreadyReported, RunOptions, Services, run_post, run_setup
from autopost.results import PublishResult
from autopost.slack_helper import SlackNotifier
from fakes import FakeWebClient, MemoryDrive, make_sheet, make_topic, post_json

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


class StubSelector:
    def __init__(self):
        self.calls = []

    def pick(self, history, now, requested=""):
        self.calls.append(requested)
        return make_topic(requested=bool(requested)), 12


class StubResearcher:
    def __init__(self, error=None):
        self.error = error

    def research(self, topic, today):
        if self.error:
            raise self.error
        return make_sheet(topic=topic)


class StubWriter:
    def __init__(self):
        self.revisions = 0

    def write(self, sheet, style, today):
        return parse_post(post_json())

    def revise(self, content, sheet, issues):
        self.revisions += 1
        return parse_post(post_json(title=f"🤖 수정본 {self.revisions}"))


class StubChecker:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def check(self, content, sheet, today, rounds=1):
        ok = self.outcomes.pop(0)
        verdict = ClaimVerdict("주장", "supported" if ok else "contradicted", "https://x", "" if ok else "고침")
        return FactCheckReport((verdict,), rounds)


class StubBlogger:
    def __init__(self, fail=False):
        self.fail = fail
        self.posts = []

    def publish(self, title, body, labels):
        if self.fail:
            raise RuntimeError("blogger down")
        self.posts.append((title, body, labels))
        return PublishResult("구글 블로그", True, "https://ai.blogspot.com/p/1", "발행 완료")


class StubNaver:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def publish(self, content, extras):
        if self.fail:
            raise RuntimeError("naver down")
        self.calls.append((content, extras))
        return PublishResult("네이버 블로그", True, "https://blog.naver.com/me/223456789012", "발행 완료")


def cfg(tmp_path, **env):
    base = {"OUTPUT_DIR": str(tmp_path / "out"), "MIN_BODY_CHARS": "1000", "SLACK_CHANNEL_DRAFT": "CDRAFT"}
    return load_config({**base, **env})


def services(drive, client, checker=(True,), researcher=None, blogger=None, naver=None, writer=None):
    return Services(
        drive=drive, slack=SlackNotifier(channel_id="C1", client=client), selector=StubSelector(),
        researcher=researcher or StubResearcher(), writer=writer or StubWriter(), checker=StubChecker(checker),
        blogger=blogger, naver=naver, now=lambda: NOW,
    )


def history_of(drive):
    root = next(f for f in drive.children("root") if f.is_folder)
    found = drive.find_child(root.id, HISTORY_FILE)
    return PostHistory.from_json(drive.read_text(found.id))


def test_publishes_to_both_platforms_and_records_history(tmp_path):
    drive, client = MemoryDrive(), FakeWebClient()
    blogger, naver = StubBlogger(), StubNaver()
    code = run_post(cfg(tmp_path), RunOptions(trigger="schedule"), services(drive, client, blogger=blogger, naver=naver))
    assert code == 0
    title, body, labels = blogger.posts[0]
    assert title.startswith("🤖 Gemini 3.8 Live") and labels[0] == "AI 언어 모델"
    assert "lh3.googleusercontent.com" in body and "📚 참고 문헌 및 출처" in body  # 대표 이미지·참고문헌
    assert "Google DeepMind. (2026, September 24)" in body and "교차 확인했습니다" in body
    assert naver.calls[0][1].cover.name == "cover.png" and "2026년 9월 29일" in naver.calls[0][1].notes
    entry = history_of(drive).entries[0]
    assert entry.urls == {"blogger": "https://ai.blogspot.com/p/1", "naver": "https://blog.naver.com/me/223456789012"}
    progress = next(i for i, m in enumerate(client.messages, start=1) if m["text"].startswith("🤖 AI 포스팅 진행 중"))
    assert client.last_text(f"1700000000.{progress:06d}").startswith("✅ 포스팅 완료")
    uploads = {u["title"] for u in client.uploads}
    assert {"출처·사실 목록", "원고", "사실 검증 보고", "대표 이미지", "구글 블로그 미리보기", "네이버 블로그 미리보기"} <= uploads
    assert any(u["channel"] == "CDRAFT" for u in client.uploads)


def test_related_posts_come_from_history(tmp_path):
    drive, client = MemoryDrive(), FakeWebClient()
    blogger = StubBlogger()
    svc = services(drive, client, blogger=blogger)
    run_post(cfg(tmp_path), RunOptions(), svc)
    run_post(cfg(tmp_path), RunOptions(), services(drive, client, blogger=blogger))
    assert "🔗 함께 보면 좋은 글" in blogger.posts[1][1] and "https://ai.blogspot.com/p/1" in blogger.posts[1][1]
    assert len(history_of(drive).entries) == 2


def test_fact_check_failure_is_fixed_then_published(tmp_path):
    drive, client = MemoryDrive(), FakeWebClient()
    writer, blogger = StubWriter(), StubBlogger()
    code = run_post(cfg(tmp_path), RunOptions(), services(drive, client, checker=(False, True), writer=writer, blogger=blogger))
    assert code == 0 and writer.revisions == 1
    assert blogger.posts[0][0] == "🤖 수정본 1"


def test_unfixable_facts_hold_publishing(tmp_path):
    drive, client = MemoryDrive(), FakeWebClient()
    blogger = StubBlogger()
    code = run_post(cfg(tmp_path, MAX_FIX_ROUNDS="1"), RunOptions(),
                    services(drive, client, checker=(False, False), blogger=blogger))
    assert code == 1 and blogger.posts == []
    assert history_of(drive).entries == ()
    assert any("사실 검증 미통과" in t for t in client.texts())


def test_dry_run_makes_previews_without_publishing(tmp_path):
    drive, client = MemoryDrive(), FakeWebClient()
    blogger = StubBlogger()
    code = run_post(cfg(tmp_path), RunOptions(dry_run=True, topic="GPT-6", requested_by="<!channel>"),
                    services(drive, client, blogger=blogger))
    assert code == 0 and blogger.posts == [] and history_of(drive).entries == ()
    assert any("테스트 완료" in t for t in client.texts())
    assert "&lt;!channel&gt;" in json.dumps(client.messages[1]["blocks"], ensure_ascii=False)


def test_platform_failures_are_isolated_and_naver_fallback_posted(tmp_path):
    drive, client = MemoryDrive(), FakeWebClient()
    code = run_post(cfg(tmp_path), RunOptions(), services(drive, client, blogger=StubBlogger(), naver=StubNaver(fail=True)))
    assert code == 0
    assert any("수동 발행용 원고" in t for t in client.texts())
    assert history_of(drive).entries[0].urls == {"blogger": "https://ai.blogspot.com/p/1"}


def test_all_platforms_failing_returns_error_without_record(tmp_path):
    drive, client = MemoryDrive(), FakeWebClient()
    code = run_post(cfg(tmp_path), RunOptions(), services(drive, client, blogger=StubBlogger(fail=True)))
    assert code == 1 and history_of(drive).entries == ()


def test_step_error_is_reported_once(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    drive, client = MemoryDrive(), FakeWebClient()
    with pytest.raises(AlreadyReported):
        run_post(cfg(tmp_path), RunOptions(), services(drive, client, researcher=StubResearcher(RuntimeError("no data"))))
    assert "자료 조사·출처 확인" in summary.read_text(encoding="utf-8")
    assert any("🚨 '자료 조사·출처 확인' 단계 오류" in t for t in client.texts())


def test_draft_is_kept_when_fact_check_cannot_finish(tmp_path):
    class DownChecker:
        def check(self, *args, **kwargs):
            raise RuntimeError("503 UNAVAILABLE")

    drive, client = MemoryDrive(), FakeWebClient()
    blogger = StubBlogger()
    svc = services(drive, client, blogger=blogger)
    with pytest.raises(AlreadyReported):
        run_post(cfg(tmp_path), RunOptions(), Services(**{**svc.__dict__, "checker": DownChecker()}))
    assert blogger.posts == [] and history_of(drive).entries == ()
    kept = [u for u in client.uploads if u["title"] == "사실 검증 전 원고"]
    assert kept and "발행하지 않았습니다" in kept[0]["initial_comment"]


def test_untrusted_titles_cannot_mention_the_channel(tmp_path):
    class HostileSelector(StubSelector):
        def pick(self, history, now, requested=""):
            return make_topic(title="<!channel> <https://evil.com|클릭>", source_url="https://x.com/<!here>"), 3

    drive, client = MemoryDrive(), FakeWebClient()
    svc = services(drive, client, blogger=StubBlogger())
    hostile = Services(**{**svc.__dict__, "selector": HostileSelector()})
    run_post(cfg(tmp_path), RunOptions(), hostile)
    sent = " ".join(client.texts() + [u.get("initial_comment", "") for u in client.uploads])
    sent += json.dumps([m.get("blocks") for m in client.messages + client.updates], ensure_ascii=False)
    assert "<!channel>" not in sent and "<!here>" not in sent and "<https://evil.com" not in sent
    assert "&lt;!channel&gt;" in sent


def test_setup_reports_readiness(tmp_path):
    client = FakeWebClient()
    code = run_setup(cfg(tmp_path), Services(drive=MemoryDrive(), slack=SlackNotifier(channel_id="C1", client=client)))
    assert code == 0 and "초기 세팅 완료" in client.messages[0]["text"]


def test_post_requires_ai_services(tmp_path):
    with pytest.raises(RuntimeError, match="서비스"):
        run_post(cfg(tmp_path), RunOptions(), Services(drive=MemoryDrive(), slack=SlackNotifier()))


def test_history_entries_are_read_only():
    entry = HistoryEntry("d", "t", "t", "c", "", MappingProxyType({"naver": "u"}))
    with pytest.raises(TypeError):
        entry.urls["naver"] = "x"
