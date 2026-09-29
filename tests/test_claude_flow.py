"""Claude 원고 흐름 — 원고 폴더 → 발행 준비 → 슬랙 승인 요청 → (✅) → 발행. 구글 AI 호출 없음."""
import json
from datetime import datetime, timezone

import pytest
from PIL import Image

from autopost.claude_draft import DraftError, DraftPreparer, build_draft_blocks, fact_line, load_draft, send_for_approval
from autopost.draft_store import DraftStore, DraftStoreError
from autopost.links import LinkCheck
from autopost.publish_draft import main as publish_main
from autopost.publish_draft import publish_draft
from autopost.research import QuoteImage
from autopost.slack_helper import SlackNotifier
from fakes import FakeBlogger, FakeImageHost, FakeWebClient, post_dict

NOW = datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc)
OFFICIAL = "https://www.nvidia.com/en-us/data-center/vera-cpu/"
DEAD = "https://made-up.example/404"


def write_draft(folder, images=(), **post_overrides):
    data = post_dict(**post_overrides)
    data["category"] = post_overrides.get("category", "AI 뉴스 리뷰")
    data["sections"][0]["blocks"].insert(1, {"type": "image", "image_id": "I1"})
    data["source_ids"] = ["S1", "S2"]
    (folder / "post.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    research = {
        "topic": "NVIDIA Vera CPU 공개", "source_url": OFFICIAL,
        "sources": [{"id": "S1", "title": "NVIDIA Vera CPU", "url": OFFICIAL, "publisher": "NVIDIA",
                     "published": "2026-03-16", "official": True},
                    {"id": "S2", "title": "없는 기사", "url": DEAD, "publisher": "?"},
                    {"id": "S3", "title": "주소 없음", "url": "not-a-url"}],
        "images": list(images),
        "factcheck": {"claims": 40, "confirmed": 39, "removed": ["확인 안 된 문장"]},
    }
    (folder / "research.json").write_text(json.dumps(research, ensure_ascii=False), encoding="utf-8")
    return folder


class Collector:
    def collect(self, candidates, sources, workdir):
        found = []
        for c in candidates:
            path = workdir / f"quote_{c.id}.jpg"
            Image.new("RGB", (800, 450), "black").save(path)
            found.append(QuoteImage(c.id, c.caption, "NVIDIA", c.page_url, path, 800, 450))
        return tuple(found)


def preparer(blogger=None, host=None):
    checks = {OFFICIAL: LinkCheck(OFFICIAL, 403), DEAD: LinkCheck(DEAD, 404)}
    return DraftPreparer("AI백과사전", blogger or FakeBlogger(), host or FakeImageHost(), Collector(),
                         checker=lambda url, keywords, session: checks[url], now=lambda: NOW)


IMAGE = {"id": "I1", "image_url": "", "page_url": OFFICIAL, "caption": "Vera CPU 제품 사진", "credit": "NVIDIA"}


class MemoryStore:
    def __init__(self):
        self.items = {}
        self.puts = []

    def put(self, draft_id, data):
        self.puts.append((draft_id, data))
        self.items[draft_id] = data

    def get(self, draft_id):
        if draft_id not in self.items:
            raise DraftStoreError("없음")
        return self.items[draft_id]


def test_prepare_builds_publishable_html_with_hosted_images_and_references(tmp_path):
    host = FakeImageHost()
    prepared = preparer(host=host).prepare(write_draft(tmp_path, [IMAGE]), "20260929_1300_4")
    html = prepared.html
    assert "https://img.example/img/20260929_1300_cover.png" in html and "20260929_1300_I1.jpg" in html
    assert "▲ Vera CPU 제품 사진 (출처:" in html and "📚 참고 문헌 및 출처" in html
    assert "NVIDIA. (2026, March 16). <i>NVIDIA Vera CPU</i>" in html and DEAD not in html  # 열리지 않는 출처는 제외
    assert r'"category":"AI \ub274\uc2a4 \ub9ac\ubdf0"}-->' in html  # 발행 기록 표시
    assert prepared.labels[0] == "AI 뉴스 리뷰" and prepared.content.closing.endswith("감사합니다.")
    assert any("made-up.example" in w for w in prepared.warnings)
    assert prepared.fact_line == "주장 40개 중 39개 확인 (Claude 검증) · 확인 안 된 1곳 뺌"
    assert (tmp_path / "publish.html").is_file() and prepared.preview.is_file()
    bundle = prepared.bundle(NOW)
    assert bundle["status"] == "pending" and bundle["title"] == prepared.content.title and bundle["labels"][0] == "AI 뉴스 리뷰"


def test_images_that_fail_are_dropped_and_duplicates_are_refused(tmp_path):
    class NoImages(Collector):
        def collect(self, candidates, sources, workdir):
            return ()

    p = preparer()
    p._collector = NoImages()
    prepared = p.prepare(write_draft(tmp_path, [IMAGE]), "d1")
    assert "<figure" not in prepared.html and any("인용 이미지 1개" in w for w in prepared.warnings)

    blogger = FakeBlogger()
    blogger.publish("예전 글", '<!--yoopost {"topic":"NVIDIA Vera CPU 공개","source":"' + OFFICIAL + '"}-->', ["AI 도구"])
    with pytest.raises(DraftError, match="이미 발행한 주제"):
        preparer(blogger=blogger).prepare(write_draft(tmp_path), "d2")
    assert preparer(blogger=blogger).prepare(write_draft(tmp_path), "d2", allow_duplicate=True).html


def test_broken_draft_folders_are_explained(tmp_path):
    with pytest.raises(DraftError, match="research.json"):
        load_draft(tmp_path)
    (tmp_path / "research.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(DraftError, match="JSON 형식이 아닙니다"):
        load_draft(tmp_path)
    (tmp_path / "research.json").write_text("[]", encoding="utf-8")
    with pytest.raises(DraftError, match="최상위"):
        load_draft(tmp_path)
    (tmp_path / "research.json").write_text('{"sources": []}', encoding="utf-8")
    (tmp_path / "post.json").write_text('{"title": ""}', encoding="utf-8")
    with pytest.raises(DraftError, match="post.json 형식 오류"):
        load_draft(tmp_path)
    write_draft(tmp_path)
    (tmp_path / "research.json").write_text('{"sources": [{"url": "bad"}]}', encoding="utf-8")
    with pytest.raises(DraftError, match="출처"):
        load_draft(tmp_path)
    all_dead = {"sources": [{"id": "S1", "url": DEAD}]}
    (tmp_path / "research.json").write_text(json.dumps(all_dead), encoding="utf-8")
    with pytest.raises(DraftError, match="열리는 출처가 하나도"):
        preparer().prepare(tmp_path, "d")
    assert fact_line({}) == "Claude 검증"


def test_send_for_approval_posts_card_uploads_preview_and_links_message(tmp_path):
    prepared = preparer().prepare(write_draft(tmp_path, [IMAGE]), "20260929_1300_4")
    client, store = FakeWebClient(), MemoryStore()
    slack = SlackNotifier(channel_id="C1", client=client)
    ts = send_for_approval(prepared, store, slack, "CDRAFT", NOW)
    card = client.messages[0]
    assert card["channel"] == "CDRAFT" and "발행 대기 원고" in card["text"]
    blocks = json.dumps(card["blocks"], ensure_ascii=False)
    assert "✅ 반응을 누르면 구글 블로그에 발행됩니다" in blocks and "20260929_1300_4" in blocks and "참고" in blocks
    assert {u["title"] for u in client.uploads} == {"구글 블로그 미리보기", "대표 이미지", "출처·사실 검증"}
    sources = (tmp_path / "sources.md").read_text(encoding="utf-8")
    assert "(공식) NVIDIA — NVIDIA Vera CPU" in sources and "확인 안 된 문장" in sources and DEAD not in sources
    assert all(u["thread_ts"] == ts for u in client.uploads)
    assert store.puts[0][1].get("slack") is None  # 슬랙에 올리기 전에 먼저 보관
    assert store.items["20260929_1300_4"]["slack"] == {"channel": "CDRAFT", "ts": ts}
    assert build_draft_blocks(prepared)[0]["text"]["text"].startswith("📝")

    class DownSlack(SlackNotifier):
        def post(self, *a, **k):
            return None

    with pytest.raises(DraftError, match="슬랙"):
        send_for_approval(prepared, MemoryStore(), DownSlack(), "CDRAFT", NOW)


def test_publish_draft_publishes_once_and_reports_in_thread():
    store = MemoryStore()
    store.put("d1", {"title": "제목", "html": "<p>본문</p>", "labels": ["AI 도구"], "status": "approved",
                     "slack": {"channel": "CDRAFT", "ts": "1700.1"}})
    client, blogger = FakeWebClient(), FakeBlogger()
    slack = SlackNotifier(channel_id="C1", client=client)
    assert publish_draft("d1", store, blogger, slack, "U0ABC", "CPUB", now=lambda: NOW) == 0
    assert blogger.posts == [("제목", "<p>본문</p>", ["AI 도구"])]
    assert store.items["d1"]["status"] == "published" and store.items["d1"]["url"] == "https://ai.blogspot.com/p/1"
    thread, channel_note = client.messages
    assert thread["thread_ts"] == "1700.1" and thread["channel"] == "CDRAFT" and "<@U0ABC>" in thread["text"]
    assert channel_note["channel"] == "CPUB"
    assert publish_draft("d1", store, blogger, slack) == 0 and len(blogger.posts) == 1  # 두 번 발행하지 않음
    assert "이미 발행한 원고" in client.messages[-1]["text"]


def test_publish_failure_is_recorded_for_retry():
    store = MemoryStore()
    store.put("d1", {"title": "t", "html": "h", "status": "approved"})
    client = FakeWebClient()
    code = publish_draft("d1", store, FakeBlogger(fail=True), SlackNotifier(channel_id="C1", client=client))
    assert code == 1 and store.items["d1"]["status"] == "failed"
    assert "다시 누르면 다시 시도" in client.messages[0]["text"]


def test_publish_main_validates_settings(monkeypatch):
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "BLOGGER_BLOG_ID",
                "IMAGE_HOST_URL", "IMAGE_UPLOAD_TOKEN", "SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL", "MIN_REFERENCES"):
        monkeypatch.delenv(key, raising=False)
    assert publish_main(["--id", "d1"]) == 2
    monkeypatch.setenv("MIN_REFERENCES", "x")
    assert publish_main(["--id", "d1"]) == 2


class Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def put(self, url, data, timeout, headers):
        self.calls.append(("PUT", url, headers, data))
        return self.responses.pop(0)

    def get(self, url, headers, timeout):
        self.calls.append(("GET", url, headers, None))
        return self.responses.pop(0)


def test_draft_store_talks_to_the_worker_with_the_token():
    session = Session(Response(201), Response(200, {"title": "t"}), Response(500), Response(404), Response(200, []))
    store = DraftStore("https://relay.example/", "tok", session)
    store.put("../d 1", {"title": "제목"})
    method, url, headers, data = session.calls[0]
    assert url == "https://relay.example/draft/d_1" and headers["Authorization"] == "Bearer tok"
    assert json.loads(data.decode("utf-8")) == {"title": "제목"}
    assert store.get("d1") == {"title": "t"}
    with pytest.raises(DraftStoreError, match="보관 실패"):
        store.put("d1", {})
    with pytest.raises(DraftStoreError, match="찾지 못했습니다"):
        store.get("d1")
    with pytest.raises(DraftStoreError, match="형식"):
        store.get("d1")
