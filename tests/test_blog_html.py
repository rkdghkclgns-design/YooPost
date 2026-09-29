from autopost.blog_html import PostExtras, block_html, build_blog_html
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
    assert "<b>구글</b>" in html and "Google &lt;DeepMind&gt;" in html and "<i>Intro &amp; more</i>" in html
    assert 'href="https://deepmind.google/x?a=1&amp;b=2"' in html
    assert "<b>Q1. 무료인가요?</b>" in html


def test_optional_parts_are_omitted():
    html = build_blog_html(parse_post(post_json(summary=[], faq=[])))
    for marker in ("한눈에 보기", "자주 묻는 질문", "함께 보면 좋은 글", "참고 문헌", "<img"):
        assert marker not in html


def test_block_styles_match_reference_design():
    assert "#fff3cd" in block_html(Block("info", text="t", title="팁"))
    warning = block_html(Block("warning", text="t", title="⚠️ 주의"))
    assert "#f8d7da" in warning and "⚠️ ⚠️" not in warning
    assert "💡 " in block_html(Block("info", text="제목 없음"))
    table = block_html(Block("table", headers=("항목",), rows=(("<x>",),)))
    assert "#2196F3" in table and "&lt;x&gt;" in table
    code = block_html(Block("code", text="a < b"))
    assert "#1e1e1e" in code and "a &lt; b" in code
    assert block_html(Block("list", items=("하나",))).startswith("<ul")
    assert block_html(Block("subheading", text="소제목")).startswith("<h3")
    assert block_html(Block("paragraph", text="a\n\nb")).count("<p") == 2
    assert 'style="background-color:#f5f5f5' in block_html(Block("paragraph", text="`id`"))


class _Posts:
    def __init__(self):
        self.bodies = []

    def insert(self, blogId, body, isDraft):
        self.bodies.append((blogId, body, isDraft))
        return self

    def execute(self, num_retries=0):
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
