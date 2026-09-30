"""원고에 이미지가 없을 때 — 공식 출처 대표 이미지 보충 · 본문 배치 · 클라우드 품질 검사."""
import json

from autopost.cloud_drafts import quality_report
from autopost.content import Block, PostContent, Section
from autopost.quote_images import AUTO_PAGES, auto_candidates
from autopost.research import Source
from autopost.writer import place_images
from test_claude_flow import Collector, preparer
from test_claude_flow import write_draft as write_local_draft
from test_cloud_drafts import write_draft


def src(url, official=True, title="제목", publisher="회사"):
    return Source(id="S", title=title, url=url, publisher=publisher, published="", official=official)


def test_auto_candidates_use_official_pages_only():
    sources = [src("https://news.example/a", official=False), src("https://openai.com/a", title="GPT-6 발표"),
               src("https://openai.com/a"), *[src(f"https://docs.example/{i}") for i in range(6)]]
    found = auto_candidates(sources)
    assert [c.page_url for c in found][:2] == ["https://openai.com/a", "https://docs.example/0"]
    assert len(found) == AUTO_PAGES and found[0].id == "A1" and found[0].caption == "GPT-6 발표"
    assert found[0].credit == "회사" and found[0].image_url == ""
    assert auto_candidates([src("https://x.example", official=False)]) == ()


def paragraph(text="본문"):
    return Block("paragraph", text=text)


def test_place_images_after_the_first_paragraph_of_first_and_middle_sections():
    content = PostContent(title="t", category="AI 도구", intro="i", summary=(), faq=(), closing="c", hashtags=(),
                          sections=tuple(Section(f"h{i}", (Block("info", text="박스"), paragraph(), paragraph()))
                                         for i in range(4)))
    placed = place_images(content, ["A1", "A2"])
    assert [b.kind for b in placed.sections[0].blocks] == ["info", "paragraph", "image", "paragraph"]
    assert placed.sections[2].blocks[2].image_id == "A2" and content.sections[0].blocks[2].kind == "paragraph"
    single = place_images(content, ["A1"])
    assert sum(b.kind == "image" for s in single.sections for b in s.blocks) == 1
    assert place_images(content, []) is content


def test_prepare_fills_in_official_images_when_the_draft_has_none(tmp_path):
    prepared = preparer().prepare(write_local_draft(tmp_path, []), "d1")
    assert prepared.html.count("<figure") >= 1
    assert any("대표 이미지" in w for w in prepared.warnings)

    class NoImages(Collector):
        def collect(self, candidates, sources, workdir):
            return ()

    p = preparer()
    p._collector = NoImages()
    (tmp_path / "b").mkdir()
    empty = p.prepare(write_local_draft(tmp_path / "b", []), "d2")
    assert "<figure" not in empty.html and any("넣을 이미지가 없습니다" in w for w in empty.warnings)


def test_cloud_check_requires_images_or_a_reason(tmp_path):
    ok = write_draft(tmp_path / "20260930_070000_1")
    assert not [p for p in quality_report(ok, 1000).problems if "이미지" in p]
    none = write_draft(tmp_path / "20260930_070000_2", images=False)
    post = json.loads((none / "post.json").read_text(encoding="utf-8"))
    post["sections"][0]["blocks"] = [b for b in post["sections"][0]["blocks"] if b.get("type") != "image"]
    (none / "post.json").write_text(json.dumps(post, ensure_ascii=False), encoding="utf-8")
    assert any("이미지 후보" in p for p in quality_report(none, 1000).problems)
    research = json.loads((none / "research.json").read_text(encoding="utf-8"))
    (none / "research.json").write_text(json.dumps({**research, "images_none_reason": "공시 PDF 뿐이라 그림 없음"},
                                                   ensure_ascii=False), encoding="utf-8")
    assert not [p for p in quality_report(none, 1000).problems if "이미지" in p]
    orphan = write_draft(tmp_path / "20260930_070000_3")
    post = json.loads((orphan / "post.json").read_text(encoding="utf-8"))
    post["sections"][0]["blocks"] = [b for b in post["sections"][0]["blocks"] if b.get("type") != "image"]
    (orphan / "post.json").write_text(json.dumps(post, ensure_ascii=False), encoding="utf-8")
    assert any("image 블록이 없습니다" in p for p in quality_report(orphan, 1000).problems)
