"""테스트용 가짜 외부 서비스 (구글 블로그 / 이미지 저장소 / Slack / Gemini / HTTP) 와 샘플 데이터."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from autopost.research import Fact, FactSheet, Source
from autopost.topic import Topic


# ------------------------------------------------------------------ 이미지 저장소 (ImageHost 인터페이스)
class FakeImageHost:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.uploads: list[tuple[str, str]] = []

    def upload(self, path: Path, name: str) -> str:
        if self.fail:
            raise RuntimeError("host down")
        self.uploads.append((path.name, name))
        return f"https://img.example/img/{name}"

    def ping(self) -> bool:
        return not self.fail


# ------------------------------------------------------------------ 구글 블로그 (BloggerPublisher 흉내)
class FakeBlogger:
    """발행한 글을 기억하고, recent_posts 로 최신순 목록을 돌려준다 (= 발행 기록)."""

    def __init__(self, fail: bool = False, list_fail: bool = False) -> None:
        self.fail = fail
        self.list_fail = list_fail
        self.posts: list[tuple[str, str, list]] = []

    def publish(self, title, body, labels):
        from autopost.results import PublishResult

        if self.fail:
            raise RuntimeError("blogger down")
        self.posts.append((title, body, list(labels)))
        return PublishResult("구글 블로그", True, f"https://ai.blogspot.com/p/{len(self.posts)}", "발행 완료")

    def recent_posts(self, limit=200):
        if self.list_fail:
            raise RuntimeError("list down")
        items = [{"title": t, "url": f"https://ai.blogspot.com/p/{i}", "published": "2026-09-29T10:00:00+09:00",
                  "labels": labels, "content": body} for i, (t, body, labels) in enumerate(self.posts, start=1)]
        return list(reversed(items))[:limit]

    def blog_info(self):
        return {"name": "AI백과사전", "url": "https://ai.blogspot.com/", "posts": len(self.posts)}


# ------------------------------------------------------------------ Slack
@dataclass
class FakeWebClient:
    messages: list[dict] = field(default_factory=list)
    updates: list[dict] = field(default_factory=list)
    uploads: list[dict] = field(default_factory=list)
    fail: bool = False

    def chat_postMessage(self, **kwargs):
        if self.fail:
            raise RuntimeError("slack down")
        self.messages.append(kwargs)
        return {"ok": True, "ts": f"1700000000.{len(self.messages):06d}"}

    def chat_update(self, **kwargs):
        if self.fail:
            raise RuntimeError("slack down")
        self.updates.append(kwargs)
        return {"ok": True}

    def files_upload_v2(self, **kwargs):
        if self.fail:
            raise RuntimeError("slack down")
        self.uploads.append(kwargs)

    def texts(self) -> list[str]:
        return [m["text"] for m in self.messages] + [u["text"] for u in self.updates]

    def last_text(self, ts: str) -> str:
        latest = self.messages[int(ts.split(".")[1]) - 1]["text"]
        for update in self.updates:
            if update.get("ts") == ts:
                latest = update["text"]
        return latest

    def last_blocks(self, ts: str) -> list:
        latest = self.messages[int(ts.split(".")[1]) - 1].get("blocks")
        for update in self.updates:
            if update.get("ts") == ts:
                latest = update.get("blocks")
        return latest or []


@dataclass
class FakeWebhook:
    sent: list[dict] = field(default_factory=list)
    status_code: int = 200

    def send(self, **kwargs):
        self.sent.append(kwargs)
        return type("R", (), {"status_code": self.status_code})()


# ------------------------------------------------------------------ Gemini (google-genai 클라이언트 흉내)
@dataclass
class _Web:
    uri: str
    title: str = ""


@dataclass
class _Chunk:
    web: _Web


@dataclass
class _Metadata:
    grounding_chunks: list
    web_search_queries: list


@dataclass
class _Candidate:
    grounding_metadata: object = None


@dataclass
class FakeGeminiResponse:
    text: str
    candidates: list = field(default_factory=list)


def grounded(text: str, urls=(), queries=("검색어",)) -> FakeGeminiResponse:
    chunks = [_Chunk(_Web(u, f"title {i}")) for i, u in enumerate(urls)]
    return FakeGeminiResponse(text, [_Candidate(_Metadata(chunks, list(queries)))])


class FakeGeminiModels:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, FakeGeminiResponse) else FakeGeminiResponse(item)


class FakeGeminiClient:
    def __init__(self, responses: list) -> None:
        self.models = FakeGeminiModels(responses)


def fake_gemini(responses: list, models=("m1",), rounds: int = 1):
    from autopost.gemini import Gemini

    client = FakeGeminiClient(responses)
    return Gemini("key", models[0], models[1:], client=client, max_rounds=rounds, sleep=lambda s: None), client


# ------------------------------------------------------------------ HTTP (requests.Session 흉내)
@dataclass
class FakeHttpResponse:
    status_code: int = 200
    text: str = ""
    headers: dict = field(default_factory=lambda: {"Content-Type": "text/html; charset=utf-8"})
    url: str = ""
    encoding: str = "utf-8"
    body: bytes | None = None  # 이미지 등 바이너리 응답

    @property
    def content(self) -> bytes:
        return self.body if self.body is not None else self.text.encode("utf-8")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 1):
        data = self.content
        for start in range(0, len(data), chunk_size):
            yield data[start:start + chunk_size]

    def close(self) -> None:
        pass


class FakeHttp:
    """URL 별 응답. 예외 객체를 넣으면 그 예외를 발생시킨다. 없는 주소는 404."""

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.routes.get(url, FakeHttpResponse(404, "not found"))
        if isinstance(item, Exception):
            raise item
        if item.url:
            return item
        return FakeHttpResponse(item.status_code, item.text, item.headers, url, item.encoding, item.body)


# ------------------------------------------------------------------ 샘플 데이터
def make_topic(**overrides) -> Topic:
    data = dict(title="Gemini 3.8 Live 공개", category="AI 언어 모델", angle="스펙·사용법 총정리",
                keywords=("Gemini 3.8",), source_url="https://deepmind.google/blog/gemini-3-8-live/",
                source_name="Google DeepMind", official=True)
    data.update(overrides)
    return Topic(**data)


def make_sheet(**overrides) -> FactSheet:
    sources = (
        Source("S1", "Introducing Gemini 3.8 Live", "https://deepmind.google/blog/gemini-3-8-live/",
               "Google DeepMind", "2026-09-24", official=True, grounded=True),
        Source("S2", "Gemini 3.8 Live 공개", "https://www.aitimes.com/news/1", "AI타임스", "2026-09-25"),
    )
    facts = tuple(Fact(f"사실 {i}번: 2026년 9월 24일 공개", ("S1",)) for i in range(10))
    data = dict(topic=make_topic(), summary="구글이 Gemini 3.8 Live 를 공개했다.", facts=facts,
                sources=sources, unknowns=("가격",), as_of="2026-09-29")
    data.update(overrides)
    return FactSheet(**data)


def post_dict(**overrides) -> dict:
    data = {
        "title": "🤖 Gemini 3.8 Live 사용 가이드: 라이브 아바타 총정리",
        "category": "AI 언어 모델",
        "intro": "안녕하세요! 2026년 9월 24일, **구글**이 Gemini 3.8 Live 를 공개했습니다. 함께 살펴보시죠!",
        "summary": ["Gemini 3.8 Live 공개", "이 글에서 배울 것: 사용법"],
        "sections": [
            {"heading": "📰 Gemini 3.8 Live 등장 배경", "blocks": [
                {"type": "paragraph", "text": "구글은 2026년 9월 24일 발표했습니다.\n\n두 번째 문단입니다."},
                {"type": "subheading", "text": "무엇이 달라졌나"},
                {"type": "info", "title": "핵심 포인트", "text": "`gemini-3.8-live` 모델 ID 를 씁니다."},
                {"type": "warning", "title": "⚠️ 주의", "text": "가격은 아직 공개되지 않았습니다."},
            ]},
            {"heading": "⚙️ 공식 스펙 정리", "blocks": [
                {"type": "table", "headers": ["항목", "내용"], "rows": [["공개일", "2026년 9월 24일"], ["개발사"]]},
                {"type": "code", "language": "bash", "code": "pip install google-genai"},
                {"type": "list", "items": ["실시간 대화", "아바타"]},
                {"type": "unknown", "text": "알 수 없는 형식은 문단으로"},
                {"type": "table", "headers": [], "rows": []},
            ]},
        ],
        "faq": [{"q": "무료인가요?", "a": "가격은 공개되지 않았습니다."}, {"q": "", "a": "빈 질문은 버림"}],
        "closing": "지금까지 Gemini 3.8 Live 에 대해 알아보았습니다.",
        "caveat": "가격은 바뀔 수 있습니다.",
        "hashtags": ["Gemini", "#구글 AI", "Gemini", "AI"],
        "source_ids": ["s1", "S2"],
    }
    data.update(overrides)
    return data


def post_json(**overrides) -> str:
    return json.dumps(post_dict(**overrides), ensure_ascii=False)
