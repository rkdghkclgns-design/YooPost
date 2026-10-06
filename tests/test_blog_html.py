import json

from autopost.blog_html import HIGHLIGHT, ImageRef, PostExtras, block_html, build_blog_html
from autopost.blog_publisher import BloggerPublisher, build_labels
from autopost.content import Block, Reference, parse_post
from fakes import post_json


def content():
    return parse_post(post_json()).with_references(
        [Reference("Google <DeepMind>", "2026, September 24", "Intro & more", "https://deepmind.google/x?a=1&b=2")]
    )


def test_full_html_follows_reference_blog_order():
    html = build_blog_html(content(), PostExtras(
        cover_url="https://img/1", related=(("예전 글", "https://blog/1"),),
        basis_note="※ 본 글은 2026년 9월 29일 기준으로 작성되었습니다.", fact_note="교차 확인했습니다."))
    order = ["안녕하세요!", "https://img/1", "한눈에 보기", "📰 Gemini 3.8 Live 등장 배경", "❓ 자주 묻는 질문",
             "🔗 함께 보면 좋은 글", "🎯 맺음말", "📚 참고 문헌 및 출처", "기준으로 작성되었습니다. 가격은 바뀔 수 있습니다. 교차 확인했습니다."]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert f'<b style="{HIGHLIGHT}">구글</b>' in html  # 중요 내용 강조 (굵게 + 형광 배경)
    assert "Google &lt;DeepMind&gt;" in html and "<i>Intro &amp; more</i>" in html
    assert 'href="https://deepmind.google/x?a=1&amp;b=2"' in html
    assert "<b>Q1. 무료인가요?</b>" in html  # 시스템이 붙이는 굵은 글씨는 형광 배경 없음
    assert "<!--yoopost" not in html


def test_highlight_style_matches_reference_blog():
    html = block_html(Block("paragraph", text="Vera는 **에이전트 전용 CPU**입니다."))
    assert "background-color:#e3ebfd" in HIGHLIGHT and "font-weight:700" in HIGHLIGHT
    assert f'<b style="{HIGHLIGHT}">에이전트 전용 CPU</b>입니다.' in html
    table = block_html(Block("table", headers=("항목",), rows=(("**88개** 코어",),)))
    assert f'<b style="{HIGHLIGHT}">88개</b>' in table


def test_quote_images_are_rendered_once_with_credit():
    data = json.loads(post_json())
    data["sections"][0]["blocks"][1:1] = [{"type": "image", "image_id": "i1"}, {"type": "image", "image_id": "I1"},
                                          {"type": "image", "image_id": "I9"}]
    post = parse_post(json.dumps(data, ensure_ascii=False))
    image = ImageRef("I1", "https://img.example/img/a.jpg", '구조도 "A"', "NVIDIA", "https://nvidia.com/x?a=1&b=2")
    html = build_blog_html(post, PostExtras(images=(image,), marker={"topic": "T", "source": "https://s"}))
    assert html.count("<figure") == 1  # 같은 이미지 두 번·없는 번호는 건너뜀
    assert 'src="https://img.example/img/a.jpg"' in html and 'alt="구조도 &quot;A&quot;"' in html
    assert '(출처: <a href="https://nvidia.com/x?a=1&amp;b=2"' in html and ">NVIDIA</a>)" in html
    assert html.rstrip().endswith('<!--yoopost {"topic":"T","source":"https://s"}-->')
    no_page = block_html(Block("image", image_id="I1"), {"I1": ImageRef("I1", "s", "c", "", "")})
    assert "(출처: 원문)" in no_page and block_html(Block("image", image_id="I1")) == ""


def test_optional_parts_are_omitted():
    html = build_blog_html(parse_post(post_json(summary=[], faq=[])))
    for marker in ("한눈에 보기", "자주 묻는 질문", "함께 보면 좋은 글", "참고 문헌", "<img"):
        assert marker not in html


def test_block_styles_match_reference_design():
    assert "border-left:3px solid #1d52de" in block_html(Block("info", text="t", title="팁"))  # Hashnode 파란 강조
    warning = block_html(Block("warning", text="t", title="⚠️ 주의"))
    assert "border-left:3px solid #d93036" in warning and "⚠️ ⚠️" not in warning
    assert "💡 " in block_html(Block("info", text="제목 없음"))
    table = block_html(Block("table", headers=("항목",), rows=(("<x>",),)))
    assert "background-color:#f4f5f7" in table and "border-radius:14px" in table and "&lt;x&gt;" in table
    code = block_html(Block("code", text="a < b"))
    assert "background-color:#16191c" in code and "a &lt; b" in code
    assert block_html(Block("list", items=("하나",))).startswith("<ul")
    assert block_html(Block("subheading", text="소제목")).startswith("<h3")
    assert block_html(Block("paragraph", text="a\n\nb")).count("<p") == 2
    assert 'style="background-color:#f4f5f7' in block_html(Block("paragraph", text="`id`"))


class _Posts:
    def __init__(self):
        self.bodies = []

    def insert(self, blogId, body, isDraft):
        self.bodies.append((blogId, body, isDraft))
        return self

    def execute(self, num_retries=0):
        assert num_retries == 0  # insert 응답 유실은 상위 원고 ID 복구로 처리한다.
        return {"url": "https://ai.blogspot.com/p/1"}


class _Service:
    def __init__(self):
        self.posts_api = _Posts()

    def posts(self):
        return self.posts_api


def test_blogger_publish_and_labels():
    service = _Service()
    result = BloggerPublisher(service, "123").publish("제목", "<p>본문</p>", ["AI 도구"])
    assert result.ok and result.url == "https://ai.blogspot.com/p/1"
    blog_id, body, draft = service.posts_api.bodies[0]
    assert blog_id == "123" and body["labels"] == ["AI 도구"] and draft is False
    assert build_labels("AI 도구", ["#AI 도구", "#a", "#b", "#c", "#d", "#e", "#f"]) == ["AI 도구", "a", "b", "c", "d", "e"]


class _Req:
    def __init__(self, response):
        self.response = response

    def execute(self, num_retries=0):
        return self.response


class _ListService:
    """posts().list 페이지 넘김 + blogs().get 흉내."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def posts(self):
        return self

    def blogs(self):
        return self

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _Req(self.pages.pop(0))

    def get(self, **kwargs):
        return _Req({"name": "AI백과사전", "url": "https://ai.blogspot.com/", "posts": {"totalItems": 7}})


def test_recent_posts_pages_through_live_posts_and_blog_info():
    service = _ListService([{"items": [{"title": "1"}, {"title": "2"}], "nextPageToken": "t2"},
                            {"items": [{"title": "3"}]}])
    publisher = BloggerPublisher(service, "123")
    assert [p["title"] for p in publisher.recent_posts(limit=52)] == ["1", "2", "3"]
    first, second = service.calls
    assert first["status"] == "LIVE" and first["orderBy"] == "PUBLISHED"  # API 허용 값 (소문자면 거절됨)
    assert first["fetchBodies"] is True and first["maxResults"] == 50
    assert first["pageToken"] is None and second["pageToken"] == "t2" and second["maxResults"] == 50
    assert publisher.blog_info() == {"name": "AI백과사전", "url": "https://ai.blogspot.com/", "posts": 7}
    limited = BloggerPublisher(_ListService([{"items": [{"title": "a"}], "nextPageToken": "more"}]), "1")
    assert len(limited.recent_posts(limit=1)) == 1
