"""전체 흐름 — 외부 서비스는 가짜로 두고 단계 순서·발행 보류·발행 기록(블로그 글 목록)·슬랙 보고를 확인한다."""
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import MappingProxyType

import pytest
from PIL import Image

from autopost.config import load_config
from autopost.content import parse_post
from autopost.fact_check import ClaimVerdict, FactCheckReport
from autopost.history import HistoryEntry, load_history
from autopost.pipeline import AlreadyReported, RunOptions, Services, run_post, run_setup
from autopost.prune import PruneResult
from autopost.research import ImageCandidate, QuoteImage
from autopost.slack_helper import SlackNotifier
from fakes import FakeBlogger, FakeImageHost, FakeWebClient, make_sheet, make_topic, post_dict

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


class StubSelector:
    def __init__(self):
        self.histories = []

    def pick(self, history, now, requested=""):
        self.histories.append(history)
        return make_topic(requested=bool(requested)), 12


class StubResearcher:
    def __init__(self, error=None, candidates=()):
        self.error = error
        self.candidates = tuple(candidates)

    def research(self, topic, today):
        if self.error:
            raise self.error
        return make_sheet(topic=topic, image_candidates=self.candidates, engine="구글 검색")


class StubImages:
    """후보마다 작은 이미지 파일을 만들어 '확인 통과'로 돌려준다."""

    def __init__(self):
        self.calls = 0

    def collect(self, candidates, sources, workdir):
        self.calls += 1
        workdir.mkdir(parents=True, exist_ok=True)
        kept = []
        for c in candidates:
            path = workdir / f"quote_{c.id}.jpg"
            Image.new("RGB", (800, 450), "white").save(path)
            kept.append(QuoteImage(c.id, c.caption, "Google DeepMind", c.page_url, path, 800, 450))
        return tuple(kept)


class StubWriter:
    def __init__(self, with_image=False):
        self.revisions = 0
        self.with_image = with_image

    def _post(self, **overrides):
        data = post_dict(**overrides)
        if self.with_image:
            data["sections"][0]["blocks"].insert(1, {"type": "image", "image_id": "I1"})
        return parse_post(json.dumps(data, ensure_ascii=False))

    def write(self, sheet, style, today):
        return self._post()

    def revise(self, content, sheet, issues):
        self.revisions += 1
        return self._post(title=f"🤖 수정본 {self.revisions}")


class StubChecker:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def check(self, content, sheet, today, rounds=1):
        ok = self.outcomes.pop(0)
        verdict = ClaimVerdict("주장", "supported" if ok else "contradicted", "https://x", "" if ok else "고침")
        return FactCheckReport((verdict,), rounds)


class StubPruner:
    """문제 주장마다 문장 하나를 뺐다고 보고 (unlocated 로 위치 못 찾음을 흉내)."""

    def __init__(self, unlocated=(), shrink=False):
        self.unlocated = list(unlocated)
        self.shrink = shrink
        self.calls = []

    def prune(self, content, issues):
        self.calls.append([v.claim for v in issues])
        pruned = replace(content, sections=content.sections[:1]) if self.shrink else content
        missing = tuple(self.unlocated.pop(0)) if self.unlocated else ()
        return PruneResult(pruned, tuple(f"뺀 문장: {v.claim}" for v in issues), missing)


def sent(client) -> str:
    """슬랙으로 보낸 글자·블록 전체 (진행 체크리스트·결과 카드 포함)."""
    return " ".join(client.texts()) + json.dumps([m.get("blocks") for m in client.messages + client.updates],
                                                 ensure_ascii=False)


def cfg(tmp_path, **env):
    base = {"OUTPUT_DIR": str(tmp_path / "out"), "MIN_BODY_CHARS": "1000", "SLACK_CHANNEL_DRAFT": "CDRAFT"}
    return load_config({**base, **env})


def services(client, checker=(True,), researcher=None, blogger=None, writer=None, host=None, images=None,
             pruner=None):
    return Services(
        slack=SlackNotifier(channel_id="C1", client=client), selector=StubSelector(),
        researcher=researcher or StubResearcher(), writer=writer or StubWriter(), checker=StubChecker(checker),
        blogger=blogger, images=images, image_host=host, pruner=pruner, now=lambda: NOW,
    )


def test_publishes_to_google_blog_with_hosted_cover(tmp_path):
    client, blogger, host = FakeWebClient(), FakeBlogger(), FakeImageHost()
    code = run_post(cfg(tmp_path), RunOptions(trigger="schedule"), services(client, blogger=blogger, host=host))
    assert code == 0
    title, body, labels = blogger.posts[0]
    assert title.startswith("🤖 Gemini 3.8 Live") and labels[0] == "AI 언어 모델"
    assert "https://img.example/img/20260929_1000_cover.png" in body  # 대표 이미지는 이미지 저장소 주소
    assert "googleusercontent" not in body and "drive" not in body.lower()
    assert "📚 참고 문헌 및 출처" in body and "Google DeepMind. (2026, September 24)" in body
    assert "2026년 9월 29일 기준으로 작성" in body and "딥리서치" not in body
    assert "<!--yoopost " in body  # 발행 기록 표시
    assert host.uploads == [("cover.png", "20260929_1000_cover.png")]
    progress = next(i for i, m in enumerate(client.messages, start=1) if m["text"].startswith("🤖 AI 포스팅 진행 중"))
    assert client.last_text(f"1700000000.{progress:06d}").startswith("✅ 포스팅 완료")
    steps = json.dumps(client.last_blocks(f"1700000000.{progress:06d}"), ensure_ascii=False)
    assert "자료 조사·출처 확인" in steps and "딥리서치" not in steps and "발행 기록" not in steps
    uploads = {u["title"] for u in client.uploads}
    assert {"출처·사실 목록", "원고", "사실 검증 보고", "대표 이미지", "구글 블로그 미리보기"} <= uploads
    assert any(u["channel"] == "CDRAFT" for u in client.uploads)


def test_quote_images_are_collected_placed_and_uploaded(tmp_path):
    candidates = (ImageCandidate("I1", "https://deepmind.google/i1.png", "https://deepmind.google/blog/gemini-3-8-live/",
                                 "Gemini 3.8 Live 구조도"),)
    client, blogger, host, images = FakeWebClient(), FakeBlogger(), FakeImageHost(), StubImages()
    svc = services(client, blogger=blogger, host=host, images=images,
                   researcher=StubResearcher(candidates=candidates), writer=StubWriter(with_image=True))
    assert run_post(cfg(tmp_path), RunOptions(), svc) == 0
    body = blogger.posts[0][1]
    assert "https://img.example/img/20260929_1000_I1.jpg" in body and "Gemini 3.8 Live 구조도" in body
    assert "출처: <a href=\"https://deepmind.google/blog/gemini-3-8-live/\"" in body
    assert ("quote_I1.jpg", "20260929_1000_I1.jpg") in host.uploads
    assert "인용 이미지 I1" in {u["title"] for u in client.uploads}
    preview = next(p for p in (tmp_path / "out").rglob("blog_preview_google.html"))
    assert "data:image/jpeg;base64," in preview.read_text(encoding="utf-8")


def test_images_are_embedded_when_no_image_host(tmp_path):
    client, blogger = FakeWebClient(), FakeBlogger()
    assert run_post(cfg(tmp_path), RunOptions(), services(client, blogger=blogger)) == 0
    assert 'src="data:image/jpeg;base64,' in blogger.posts[0][1]


def test_history_comes_from_published_blog_posts(tmp_path):
    client, blogger = FakeWebClient(), FakeBlogger()
    run_post(cfg(tmp_path), RunOptions(), services(client, blogger=blogger, host=FakeImageHost()))
    svc = services(client, blogger=blogger, host=FakeImageHost())
    run_post(cfg(tmp_path), RunOptions(), svc)
    seen = svc.selector.histories[0]
    assert seen.entries[0].source_url == "https://deepmind.google/blog/gemini-3-8-live/"
    assert seen.is_duplicate("다른 제목", "https://deepmind.google/blog/gemini-3-8-live")
    assert "🔗 함께 보면 좋은 글" in blogger.posts[1][1] and "https://ai.blogspot.com/p/1" in blogger.posts[1][1]
    assert len(load_history(blogger).entries) == 2


def test_fact_check_failure_is_fixed_then_published(tmp_path):
    client, writer, blogger = FakeWebClient(), StubWriter(), FakeBlogger()
    code = run_post(cfg(tmp_path), RunOptions(), services(client, checker=(False, True), writer=writer, blogger=blogger))
    assert code == 0 and writer.revisions == 1
    assert blogger.posts[0][0] == "🤖 수정본 1"


def test_unfixable_facts_hold_publishing(tmp_path):
    client, blogger = FakeWebClient(), FakeBlogger()
    code = run_post(cfg(tmp_path, MAX_FIX_ROUNDS="1"), RunOptions(),
                    services(client, checker=(False, False), blogger=blogger))
    assert code == 1 and blogger.posts == []
    assert any("사실 검증 미통과" in t for t in client.texts())


def test_remaining_issues_are_removed_and_the_post_is_still_published(tmp_path):
    client, blogger, pruner = FakeWebClient(), FakeBlogger(), StubPruner()
    code = run_post(cfg(tmp_path, MAX_FIX_ROUNDS="1"), RunOptions(),
                    services(client, checker=(False, False), blogger=blogger, pruner=pruner))
    assert code == 0 and len(blogger.posts) == 1  # 발행이 멈추지 않음
    assert pruner.calls == [["주장"]]
    assert "교차 확인했습니다" in blogger.posts[0][1]  # 남은 내용은 모두 확인된 것
    fact = next(p for p in (tmp_path / "out").rglob("factcheck.md")).read_text(encoding="utf-8")
    assert "## 발행 전 제외한 문장·항목 (1곳)" in fact and "- 뺀 문장: 주장" in fact
    assert "문제 문장·항목 1곳 빼고 발행" in sent(client) and "포스팅 완료" in sent(client)
    assert any(u["title"] == "사실 검증 보고" and "✂️ 사실 검증" in u["initial_comment"] for u in client.uploads)


def test_unlocated_issue_triggers_one_more_check_then_publish(tmp_path):
    client, blogger = FakeWebClient(), FakeBlogger()
    pruner = StubPruner(unlocated=[("못 찾은 주장",)])
    code = run_post(cfg(tmp_path, MAX_FIX_ROUNDS="0"), RunOptions(),
                    services(client, checker=(False, True), blogger=blogger, pruner=pruner))
    assert code == 0 and len(blogger.posts) == 1 and len(pruner.calls) == 1  # 다시 검증해 통과


def test_still_unlocated_or_too_short_after_removal_holds_publishing(tmp_path):
    client, blogger = FakeWebClient(), FakeBlogger()
    pruner = StubPruner(unlocated=[("A",), ("B",)])
    code = run_post(cfg(tmp_path, MAX_FIX_ROUNDS="0"), RunOptions(),
                    services(client, checker=(False, False), blogger=blogger, pruner=pruner))
    assert code == 1 and blogger.posts == [] and len(pruner.calls) == 2
    assert "위치를 찾지 못한 주장 1개가 남아 발행 보류" in sent(client)

    client, blogger = FakeWebClient(), FakeBlogger()
    code = run_post(cfg(tmp_path, MAX_FIX_ROUNDS="0"), RunOptions(),
                    services(client, checker=(False,), blogger=blogger, pruner=StubPruner(shrink=True)))
    assert code == 1 and blogger.posts == []
    assert "본문이 너무 짧아져 발행 보류" in sent(client)


def test_dry_run_makes_previews_without_publishing_or_uploading(tmp_path):
    client, blogger, host = FakeWebClient(), FakeBlogger(), FakeImageHost()
    code = run_post(cfg(tmp_path), RunOptions(dry_run=True, topic="GPT-6", requested_by="<!channel>"),
                    services(client, blogger=blogger, host=host))
    assert code == 0 and blogger.posts == [] and host.uploads == []
    assert any("테스트 완료" in t for t in client.texts())
    assert "&lt;!channel&gt;" in json.dumps(client.messages[0]["blocks"], ensure_ascii=False)


def test_blog_failure_returns_error(tmp_path):
    client = FakeWebClient()
    code = run_post(cfg(tmp_path), RunOptions(), services(client, blogger=FakeBlogger(fail=True)))
    assert code == 1


def test_step_error_is_reported_once(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    client = FakeWebClient()
    with pytest.raises(AlreadyReported):
        run_post(cfg(tmp_path), RunOptions(), services(client, researcher=StubResearcher(RuntimeError("no data"))))
    assert "자료 조사·출처 확인" in summary.read_text(encoding="utf-8")
    assert any("🚨 '자료 조사·출처 확인' 단계 오류" in t for t in client.texts())


def test_draft_is_kept_when_fact_check_cannot_finish(tmp_path):
    class DownChecker:
        def check(self, *args, **kwargs):
            raise RuntimeError("503 UNAVAILABLE")

    client, blogger = FakeWebClient(), FakeBlogger()
    svc = replace(services(client, blogger=blogger), checker=DownChecker())
    with pytest.raises(AlreadyReported):
        run_post(cfg(tmp_path), RunOptions(), svc)
    assert blogger.posts == []
    kept = [u for u in client.uploads if u["title"] == "사실 검증 전 원고"]
    assert kept and "발행하지 않았습니다" in kept[0]["initial_comment"]


def test_untrusted_titles_cannot_mention_the_channel(tmp_path):
    class HostileSelector(StubSelector):
        def pick(self, history, now, requested=""):
            return make_topic(title="<!channel> <https://evil.com|클릭>", source_url="https://x.com/<!here>"), 3

    client = FakeWebClient()
    hostile = replace(services(client, blogger=FakeBlogger()), selector=HostileSelector())
    run_post(cfg(tmp_path), RunOptions(), hostile)
    sent = " ".join(client.texts() + [u.get("initial_comment", "") for u in client.uploads])
    sent += json.dumps([m.get("blocks") for m in client.messages + client.updates], ensure_ascii=False)
    assert "<!channel>" not in sent and "<!here>" not in sent and "<https://evil.com" not in sent
    assert "&lt;!channel&gt;" in sent


def test_setup_checks_blog_ai_and_image_host(tmp_path):
    client = FakeWebClient()
    ready = cfg(tmp_path, GEMINI_API_KEY="k", SLACK_BOT_TOKEN="xoxb-1", SLACK_CHANNEL_ID="C1")
    code = run_setup(ready, Services(slack=SlackNotifier(channel_id="C1", client=client), blogger=FakeBlogger(),
                                     image_host=FakeImageHost()))
    assert code == 0 and "연동 점검 완료" in client.messages[0]["text"]
    blocks = json.dumps(client.messages[0]["blocks"], ensure_ascii=False)
    assert "AI백과사전 (글 0개)" in blocks and "구글 검색" in blocks and "https://ai.blogspot.com/" in blocks

    class DownBlogger(FakeBlogger):
        def blog_info(self):
            raise RuntimeError("401 invalid_grant")

    client = FakeWebClient()
    code = run_setup(ready, Services(slack=SlackNotifier(channel_id="C1", client=client), blogger=DownBlogger(),
                                     image_host=FakeImageHost(fail=True)))
    blocks = json.dumps(client.messages[0]["blocks"], ensure_ascii=False)
    assert code == 1 and "확인 필요" in client.messages[0]["text"] and "invalid_grant" in blocks


def test_post_requires_ai_services(tmp_path):
    with pytest.raises(RuntimeError, match="서비스"):
        run_post(cfg(tmp_path), RunOptions(), Services(slack=SlackNotifier()))


def test_history_entries_are_read_only():
    entry = HistoryEntry("d", "t", "t", "c", "", MappingProxyType({"blogger": "u"}))
    with pytest.raises(TypeError):
        entry.urls["blogger"] = "x"
