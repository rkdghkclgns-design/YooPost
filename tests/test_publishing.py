from datetime import datetime, timezone

from autopost.content import parse_post
from autopost.history import PostHistory
from autopost.publishing import (
    PublishContext,
    attach_references,
    base_notes,
    publish_blogger,
    published_urls,
    skipped_results,
    write_preview,
)
from autopost.results import PublishResult
from fakes import FakeBlogger, FakeImageHost, make_sheet, post_json

NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


def ctx(cover=None):
    return PublishContext(parse_post(post_json()), cover, base_notes(NOW, True), PostHistory(), "stamp",
                          topic="원 기사", source_url="https://src")


def test_references_come_only_from_verified_sources():
    content = attach_references(parse_post(post_json(source_ids=["S2", "S9"])), make_sheet(), NOW)
    assert [r.url for r in content.references] == ["https://www.aitimes.com/news/1"]
    assert content.references[0].date == "2026, September 25"


def test_notes_depend_on_fact_check():
    notes = base_notes(NOW, fact_checked=True)
    assert notes.basis_note == "※ 본 글은 2026년 9월 29일 기준으로 작성되었습니다." and notes.fact_note
    assert base_notes(NOW, fact_checked=False).fact_note == ""


def test_publish_blogger_skips_when_not_configured_and_reports_errors():
    class Boom:
        def publish(self, *args):
            raise RuntimeError("down")

    assert publish_blogger(FakeImageHost(), None, ctx()).skipped
    failed = publish_blogger(FakeImageHost(), Boom(), ctx())
    assert not failed.ok and not failed.skipped and "down" in failed.message
    assert all(r.skipped for r in skipped_results("x"))
    assert published_urls([PublishResult("구글 블로그", True, "u"), PublishResult("기타", True, "w")]) == {"blogger": "u"}


def test_preview_is_written_and_failures_do_not_stop_the_run(tmp_path, monkeypatch):
    from autopost import publishing

    page = write_preview(ctx(), tmp_path, dry_run=True)
    assert page.name == "blog_preview_google.html" and "검토용" in page.read_text(encoding="utf-8")
    monkeypatch.setattr(publishing, "render_preview", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert write_preview(ctx(), tmp_path, dry_run=False) is None


def test_cover_falls_back_to_inline_image_when_host_fails(tmp_path):
    from PIL import Image

    cover = tmp_path / "cover.png"
    Image.new("RGB", (1200, 630), "navy").save(cover)
    blogger = FakeBlogger()
    result = publish_blogger(FakeImageHost(fail=True), blogger, ctx(cover))
    body = blogger.posts[0][1]
    assert result.ok and 'src="data:image/jpeg;base64,' in body
    assert r'<!--yoopost {"topic":"\uc6d0 \uae30\uc0ac","source":"https://src"}-->' in body
    host = FakeImageHost()
    publish_blogger(host, FakeBlogger(), ctx(cover))
    assert host.uploads == [("cover.png", "stamp_cover.png")]
