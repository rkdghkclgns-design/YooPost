from datetime import datetime, timezone

from autopost.content import parse_post
from autopost.history import PostHistory
from autopost.publishing import (
    PublishContext,
    attach_references,
    base_notes,
    notes_text,
    publish_all,
    published_urls,
    skipped_results,
    write_previews,
)
from autopost.results import PublishResult
from fakes import MemoryDrive, make_sheet, post_json

NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


def test_references_come_only_from_verified_sources():
    content = attach_references(parse_post(post_json(source_ids=["S2", "S9"])), make_sheet(), NOW)
    assert [r.url for r in content.references] == ["https://www.aitimes.com/news/1"]
    assert content.references[0].date == "2026, September 25"


def test_notes_depend_on_fact_check():
    notes = base_notes(NOW, fact_checked=True)
    assert notes.basis_note == "※ 본 글은 2026년 9월 29일 기준으로 작성되었습니다." and notes.fact_note
    assert base_notes(NOW, fact_checked=False).fact_note == ""
    assert notes_text(parse_post(post_json()), notes).count("※") == 1


def test_publish_all_skips_missing_platforms_and_isolates_errors(tmp_path):
    class Boom:
        def publish(self, *args):
            raise RuntimeError("down")

    ctx = PublishContext(parse_post(post_json()), None, base_notes(NOW, True), PostHistory(), "pub", "stamp")
    results = publish_all(MemoryDrive(), Boom(), None, ctx)
    assert [(r.ok, r.skipped) for r in results] == [(False, False), (False, True)]
    assert "down" in results[0].message
    assert all(r.skipped for r in skipped_results("x"))
    assert published_urls([PublishResult("구글 블로그", True, "u"), PublishResult("네이버 블로그", False, "v"),
                           PublishResult("기타", True, "w")]) == {"blogger": "u"}


def test_preview_failures_do_not_stop_the_run(tmp_path, monkeypatch):
    from autopost import publishing

    ctx = PublishContext(parse_post(post_json()), None, base_notes(NOW, True), PostHistory(), "pub", "stamp")
    monkeypatch.setattr(publishing, "render_preview", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    google, naver = write_previews(ctx, tmp_path, "AI백과사전", "left", dry_run=True)
    assert google is None and naver is not None and "검토용" in naver.read_text(encoding="utf-8")
