"""새 디자인 — 예전 스타일로 발행된 글을 내용 그대로 새 스타일로 바꾼다."""
from autopost import blog_html
from autopost.blog_html import PostExtras, build_blog_html
from autopost.constants import CATEGORIES
from autopost.content import parse_post
from autopost.restyle import LEGACY, find_cover, marker_category, restyle_html, versioned_key
from fakes import post_json

HOST = "https://relay.example"


def legacy_version(html: str) -> str:
    """새 디자인 HTML 을 예전 스타일 문자열로 되돌린 것 (= 예전에 발행된 글)."""
    for old, new in LEGACY:
        html = html.replace(new, old)
    return html


def test_old_posts_become_exactly_the_new_design_and_new_posts_stay_untouched():
    extras = PostExtras(cover_url=f"{HOST}/img/20260929_1413_cover.png",
                        marker={"topic": "T", "source": "https://s", "category": "AI 인사이트"})
    new_html = build_blog_html(parse_post(post_json(), "AI 인사이트"), extras)
    old_html = legacy_version(new_html)
    assert "#ede9fe" in old_html and "#2196F3" in old_html and old_html != new_html
    result = restyle_html(old_html)
    assert result.html == new_html and result.changed and result.changes > 10
    again = restyle_html(new_html)
    assert again.changes == 0 and again.html == new_html  # 두 번 바꿔도 그대로
    assert blog_html.HIGHLIGHT in result.html


def test_cover_and_category_are_read_from_the_post():
    html = (f'<p>안녕</p><div><img src="{HOST}/img/20260928_0912_cover.png" alt="🤖 A &amp; B"/></div>'
            '<!--yoopost {"topic":"T","category":"AI \ubcf4\uc548"}-->')
    cover = find_cover(html, HOST + "/")
    assert cover.key == "20260928_0912_cover.png" and cover.title == "🤖 A & B" and cover.date_text == "2026.09.28"
    assert find_cover('<img src="https://elsewhere/x_cover.png" alt="x"/>', HOST) is None
    assert versioned_key(cover.key) == "20260928_0912_cover_v2.png"
    v2 = html.replace(cover.key, versioned_key(cover.key))
    assert find_cover(v2, HOST) is None  # 이미 바꾼 대표 이미지는 다시 바꾸지 않는다
    assert marker_category(html, [], CATEGORIES) == "AI 보안"
    assert marker_category("<!--yoopost {bad-->", ["AI 도구"], CATEGORIES) == "AI 도구"
    assert marker_category("", ["기타"], CATEGORIES) == CATEGORIES[0]
