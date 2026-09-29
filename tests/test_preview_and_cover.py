from PIL import Image, ImageDraw

from autopost.blog_html import PostExtras
from autopost.content import parse_post
from autopost.cover import HEIGHT, WIDTH, make_cover, strip_emoji, wrap_title
from autopost.fonts import load_font
from autopost.naver_publisher import NaverExtras
from autopost.preview_page import render_naver_preview, render_preview, write_page
from fakes import post_json


def test_cover_is_1200x630_png(tmp_path):
    path = make_cover(tmp_path / "c" / "cover.png", "🤖 " + "아주 긴 제목 " * 20, "AI 언어 모델", "AI백과사전", "2026.09.29")
    with Image.open(path) as image:
        assert image.size == (WIDTH, HEIGHT) and image.format == "PNG"


def test_wrap_title_limits_lines_and_splits_long_words():
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = load_font(64)
    lines = wrap_title(draw, "가" * 80, font, 400, max_lines=2)
    assert len(lines) == 2 and lines[-1].endswith("…")
    assert wrap_title(draw, "짧은 제목", font, 1000) == ["짧은 제목"]
    assert strip_emoji("🤖 GPT-6 ⚡ 공개") == "GPT-6 공개"


def test_google_preview_embeds_cover(tmp_path):
    cover = make_cover(tmp_path / "cover.png", "제목", "AI 도구", "AI백과사전", "2026.09.29")
    page = render_preview(parse_post(post_json()), PostExtras(basis_note="기준일"), cover, "검토용")
    assert "data:image/jpeg;base64," in page and "검토용" in page and "한눈에 보기" in page
    no_cover = render_preview(parse_post(post_json()), PostExtras(), None, "검토용")
    assert "data:image" not in no_cover


def test_naver_preview_shows_editor_lines(tmp_path):
    cover = make_cover(tmp_path / "cover.png", "제목", "AI 도구", "AI백과사전", "2026.09.29")
    page = render_naver_preview(parse_post(post_json()), NaverExtras(cover, notes="기준일"), "검토용", "AI백과사전", "center")
    assert "text-align:center" in page and "<b>구글</b>" in page and "━━" in page
    assert 'class="code">pip install google-genai' in page and "▶ 무엇이 달라졌나" in page
    missing = render_naver_preview(parse_post(post_json()), NaverExtras(tmp_path / "none.png"), "n", "b")
    assert "data:image" not in missing
    out = write_page(tmp_path / "x" / "p.html", page)
    assert out.read_text(encoding="utf-8") == page
