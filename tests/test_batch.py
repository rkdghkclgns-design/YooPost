"""카테고리별 포스팅 — 카테고리마다 1편씩 차례로, 실패해도 계속, 결과 카드 1장."""
import json
from datetime import datetime, timezone

from autopost.batch import build_batch_summary, run_categories
from autopost.constants import CATEGORIES
from autopost.gemini import Usage
from autopost.pipeline import RunOptions, Services
from autopost.slack_helper import SlackNotifier
from fakes import FakeBlogger, FakeImageHost, FakeWebClient
from test_pipeline import StubChecker, StubResearcher, StubSelector, StubWriter, cfg

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


class Meter:
    """글마다 AI 사용량이 늘어나는 흉내."""

    def __init__(self):
        self.total = Usage()

    def __call__(self):
        self.total = self.total + Usage(requests=10, input_tokens=100_000, output_tokens=50_000, searches=20)
        return self.total


class FailingResearcher(StubResearcher):
    def research(self, topic, today):
        if topic.category == "AI 공모주 분석":
            raise RuntimeError("확인된 자료가 부족합니다")
        return super().research(topic, today)


def services(client, blogger, selector, researcher=None, checker=None):
    return Services(
        slack=SlackNotifier(channel_id="C1", client=client), selector=selector,
        researcher=researcher or StubResearcher(), writer=StubWriter(),
        checker=checker or StubChecker([True] * 20), blogger=blogger, image_host=FakeImageHost(),
        usage=Meter(), now=lambda: NOW,
    )


def test_one_post_per_category_in_menu_order(tmp_path):
    client, blogger, selector = FakeWebClient(), FakeBlogger(), StubSelector()
    code = run_categories(cfg(tmp_path), RunOptions(topic="무시됨"), services(client, blogger, selector))
    assert code == 0 and len(blogger.posts) == len(CATEGORIES) == 7
    assert selector.categories == list(CATEGORIES)  # 메뉴 순서대로 1편씩
    assert [labels[0] for _, _, labels in blogger.posts] == list(CATEGORIES)  # 원고가 다르게 골라도 고정
    assert "투자 권유가 아닙니다" in blogger.posts[-1][1]  # 공모주 글 안내문
    # 앞에서 고른 주제는 뒤 카테고리의 기록에 들어가 다시 고르지 않는다
    last = selector.histories[-1]
    assert last.is_duplicate("다른 제목", "https://news.example/1")
    card = client.messages[-1]
    blocks = json.dumps(card["blocks"], ensure_ascii=False)
    assert "카테고리별 포스팅 결과" in card["text"] and "7개 카테고리 중 7개 발행" in blocks
    assert "AI 공모주 분석" in blocks and "https://ai.blogspot.com/p/7" in blocks
    assert "합계 AI 요청" in blocks and "유료 환산 약 $" in blocks
    assert len(list((tmp_path / "out").glob("*_ai_*"))) == 7  # 카테고리마다 따로 작업 폴더


def test_a_failing_category_does_not_stop_the_rest(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    client, blogger = FakeWebClient(), FakeBlogger()
    svc = services(client, blogger, StubSelector(), researcher=FailingResearcher())
    code = run_categories(cfg(tmp_path), RunOptions(), svc)
    assert code == 0 and len(blogger.posts) == 6
    blocks = json.dumps(client.messages[-1]["blocks"], ensure_ascii=False)
    assert "7개 카테고리 중 6개 발행" in blocks and "❌ *AI 공모주 분석*" in blocks and "자료가 부족" in blocks
    text = summary.read_text(encoding="utf-8")
    assert "## 📚 카테고리별 포스팅 결과" in text and "| AI 공모주 분석 | ⚠️ 오류:" in text


def test_dry_run_and_all_failed(tmp_path):
    client, blogger = FakeWebClient(), FakeBlogger()
    code = run_categories(cfg(tmp_path), RunOptions(dry_run=True), services(client, blogger, StubSelector()),
                          categories=CATEGORIES[:2])
    assert code == 0 and blogger.posts == []
    assert "카테고리별 테스트 결과" in json.dumps(client.messages[-1]["blocks"], ensure_ascii=False)

    class Broken(StubSelector):
        def pick(self, *args, **kwargs):
            raise RuntimeError("주제 없음")

    client = FakeWebClient()
    svc = Services(slack=SlackNotifier(channel_id="C1", client=client), selector=Broken(),
                   researcher=StubResearcher(), writer=StubWriter(), checker=StubChecker([]), now=lambda: NOW)
    assert run_categories(cfg(tmp_path), RunOptions(), svc, categories=CATEGORIES[:1]) == 1


def test_unexpected_errors_are_reported_and_skipped(tmp_path, monkeypatch):
    from autopost import batch

    client = FakeWebClient()
    monkeypatch.setattr(batch, "post_once", lambda *a: (_ for _ in ()).throw(ValueError("버그")))
    svc = services(client, FakeBlogger(), StubSelector())
    assert run_categories(cfg(tmp_path), RunOptions(), svc, categories=CATEGORIES[:1]) == 1
    assert any("버그" in m["text"] for m in client.messages)
    assert "| AI 언어 모델 | ⚠️ 오류: 버그 |" in build_batch_summary(
        [batch.PostOutcome(1, "AI 언어 모델", status="오류: 버그")], dry_run=False)
