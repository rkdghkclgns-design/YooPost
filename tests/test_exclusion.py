"""겹치는 원고 자동 제외 — 승인 대기 중에도, ✅ 를 누른 뒤 발행 직전에도 (2026-10-01 사용자 지시)."""
import json
from datetime import datetime, timedelta, timezone

from autopost.cloud_drafts import is_parked, read_json, write_json
from autopost.exclusion import EXCLUDED, failure_text, record_entry, sweep
from autopost.history import PostHistory, history_from_posts
from autopost.publish_draft import publish_draft
from autopost.slack_helper import SlackNotifier
from fakes import FakeBlogger, FakeWebClient
from test_claude_flow import MemoryStore
from test_cloud_drafts import write_draft

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=KST)
AMD = "https://ir.amd.com/news/world-labs"


def marker_html(topic, source, sources=()):
    data = {"topic": topic, "source": source, "category": "AI 뉴스 리뷰", **({"sources": list(sources)} if sources else {})}
    return f"<p>본문</p><!--yoopost {json.dumps(data, ensure_ascii=True)}-->"


def published_blog(*posts):
    blogger = FakeBlogger()
    for title, topic, source in posts:
        blogger.publish(title, marker_html(topic, source), ["AI 뉴스 리뷰"])
    return blogger


def card(title, topic, source, status="pending", ts="1700.1", **extra):
    return {"title": title, "html": marker_html(topic, source), "labels": ["AI 뉴스 리뷰"], "status": status,
            "topic": topic, "source_url": source, "slack": {"channel": "CDRAFT", "ts": ts}, **extra}


def test_approved_overlapping_draft_is_not_published_and_the_card_says_so():
    blogger = published_blog(("🤝 AMD의 월드 랩스 인수 총정리", "AMD to Acquire World Labs", AMD))
    store = MemoryStore()
    store.put("d2", card("🤝 AMD·World Labs 인수 총정리", "AMD to Acquire World Labs (AMD, 2026-09-29)", AMD,
                         status="approved"))
    client = FakeWebClient()
    code = publish_draft("d2", store, blogger, SlackNotifier(channel_id="C", client=client), "U1", "CPUB", now=lambda: NOW)
    assert code == 0 and len(blogger.posts) == 1  # ✅ 를 눌렀어도 새로 발행하지 않음
    assert store.items["d2"]["status"] == EXCLUDED and "AMD의 월드 랩스" in store.items["d2"]["excluded_reason"]
    assert client.updates[0]["ts"] == "1700.1" and "자동으로 제외" in client.updates[0]["text"]
    assert client.messages[-1]["thread_ts"] == "1700.1"
    assert not any(m.get("channel") == "CPUB" for m in client.messages)  # 발행 완료 채널에는 아무것도 없음
    assert publish_draft("d2", store, blogger, SlackNotifier(channel_id="C", client=client)) == 0  # 다시 눌러도
    assert len(blogger.posts) == 1 and "제외된 원고" in client.messages[-1]["text"]


def test_publish_waits_when_the_blog_list_cannot_be_read():
    store = MemoryStore()
    store.put("d1", card("t", "topic", "https://a.com/x", status="approved"))
    client = FakeWebClient()
    code = publish_draft("d1", store, FakeBlogger(list_fail=True), SlackNotifier(channel_id="C", client=client))
    assert code == 1 and store.items["d1"]["status"] == "failed"  # 확인 없이 올리지 않고, 다시 누르면 재시도
    assert "겹침 확인을 하지 못해" in client.messages[0]["text"]


def test_sweep_excludes_pending_cards_that_overlap_published_posts_or_earlier_cards(tmp_path):
    root = tmp_path / "branch"
    for name in ("20261001_070000_4", "20261001_071000_6", "20261001_072000_2", "20261001_080000_4", "20261001_090000_1"):
        write_draft(root / "drafts" / name)
    history = history_from_posts(published_blog(("🤝 AMD의 월드 랩스 인수 총정리", "AMD to Acquire World Labs", AMD)).recent_posts())
    store = MemoryStore()
    store.put("20261001_070000_4", card("AMD·World Labs 인수", "AMD to Acquire World Labs", AMD, ts="1.1"))  # 발행 글과 겹침
    store.put("20261001_071000_6", card("Pew 설문 실험", "Can AI Stand In for Human Survey Takers", "https://pew.org/x",
                                        ts="1.2"))
    store.put("20261001_072000_2", card("Pew AI 설문 응답자 실험", "Can AI Stand In for Human Survey Takers",
                                        "https://pew.org/x", ts="1.3"))  # 먼저 보낸 원고와 겹침
    store.put("20261001_080000_4", card("Ortet 출범", "Ortet Launches", "https://ortet.ai/news", status="approved",
                                        ts="1.4"))  # ✅ 했고 겹치지 않는 원고는 그대로
    store.put("20261001_090000_1", card("예전에 제외한 원고", "x", "https://x.com/y", status=EXCLUDED, ts="1.5",
                                        excluded_reason="발행한 글 'x'"))
    client = FakeWebClient()
    store.put("20261001_090500_6", card("MS AI 확산", "AI Diffusion Report", "https://microsoft.com/ai-diffusion",
                                        status="approved", ts="1.6"))  # ✅ 했지만 발행이 멈춘 겹침 원고
    write_draft(root / "drafts" / "20261001_090500_6")
    history = history_from_posts(published_blog(
        ("🤝 AMD의 월드 랩스 인수 총정리", "AMD to Acquire World Labs", AMD),
        ("📊 마이크로소프트 AI 확산 보고서", "AI Diffusion Report", "https://microsoft.com/ai-diffusion")).recent_posts())
    excluded = sweep(root, NOW, store, history, SlackNotifier(channel_id="C", client=client))
    assert excluded == ["20261001_070000_4", "20261001_072000_2", "20261001_090500_6"]
    assert store.items["20261001_071000_6"]["status"] == "pending" and store.items["20261001_080000_4"]["status"] == "approved"
    assert {u["ts"] for u in client.updates} == {"1.1", "1.3", "1.6"}
    failures = read_json(root / "state" / "failures.json")
    for name in ("20261001_070000_4", "20261001_072000_2", "20261001_090000_1"):  # 빈 카테고리로 쳐서 다른 소식으로 다시
        assert is_parked(root / "drafts" / name, failures), name
    assert sweep(root, NOW, store, history, SlackNotifier(channel_id="C", client=client)) == []  # 한 번만


def test_records_read_sources_from_the_marker_and_alerts_say_excluded():
    entry = record_entry("d", {"title": "t", "html": marker_html("topic", "https://a/1", ["https://a/1", "https://b/2"])})
    assert entry.topic == "topic" and entry.source_url == "https://a/1" and entry.sources == ("https://a/1", "https://b/2")
    assert "겹치는 원고라 자동으로 제외" in failure_text("d", "이미 쓴 글과 겹칩니다 — 발행한 글 'x'")
    assert "최신 이슈가 아니라 자동으로 제외" in failure_text("d", "최신 이슈가 아닙니다 — 오래됨")
    assert "보내지 못했습니다" in failure_text("d", "슬랙 오류")
    assert PostHistory().find_duplicate("t") is None


def test_sync_keeps_parked_exclusions_for_the_gap_count(tmp_path):
    root = tmp_path / "branch"
    write_json(root / "state" / "failures.json", {})
    folder = write_draft(root / "drafts" / "20261001_070000_4")
    store = MemoryStore()
    store.put(folder.name, card("AMD·World Labs 인수", "AMD to Acquire World Labs", AMD))
    history = history_from_posts(published_blog(("AMD의 월드 랩스 인수", "AMD to Acquire World Labs", AMD)).recent_posts())
    sweep(root, NOW, store, history, SlackNotifier(channel_id="C", client=FakeWebClient()))
    from autopost.cloud_drafts import missing_categories

    from autopost.constants import CATEGORIES

    gaps = missing_categories(root / "drafts", "20261001", CATEGORIES, read_json(root / "state" / "failures.json"))
    assert any(g.number == 4 for g in gaps)  # 제외한 원고의 카테고리는 다시 채울 빈칸


def test_old_records_without_sources_use_the_branch_research(tmp_path):
    from test_cloud_drafts import OFFICIAL

    root = tmp_path / "branch"
    folder = write_draft(root / "drafts" / "20261001_074227_6")  # research.json 출처에 OFFICIAL 이 있음
    store = MemoryStore()
    record = card("마이크로소프트 AI 확산 Q2", "Global AI Diffusion Q2 2026", "https://microsoft.com/q2", status="approved")
    record["html"] = "<p>예전 원고 — 표시 주석에 출처 목록 없음</p>"
    store.put(folder.name, record)
    history = history_from_posts(published_blog(("📊 Vera CPU 공개 총정리", "NVIDIA Vera CPU 공개", OFFICIAL)).recent_posts())
    assert sweep(root, NOW, store, history, SlackNotifier(channel_id="C", client=FakeWebClient())) == []  # 주제는 다름
    published = history_from_posts(published_blog(
        ("📊 마이크로소프트 AI 확산 Q2 총정리", "Global AI Diffusion Q2 2026", "https://microsoft.com/blog/q2")).recent_posts())
    write_json(root / "drafts" / folder.name / "research.json", {
        "topic": "Global AI Diffusion Q2 2026", "source_url": "https://microsoft.com/q2",
        "sources": [{"id": "S1", "url": "https://microsoft.com/q2"}, {"id": "S2", "url": "https://microsoft.com/blog/q2"}]})
    assert sweep(root, NOW, store, published, SlackNotifier(channel_id="C", client=FakeWebClient())) == [folder.name]
