import json

import pytest

from autopost.content import ContentError, parse_post
from autopost.fact_check import BATCH, ClaimVerdict, FactChecker, FactCheckReport
from fakes import fake_gemini, grounded, make_sheet, post_json


def claims_json(n):
    return json.dumps({"claims": [f"주장 {i}" for i in range(n)] + ["주장 0"]})


def verdicts_json(numbers, verdict="supported", **extra):
    return json.dumps({"results": [{"n": n, "verdict": verdict, "evidence": "https://x", **extra} for n in numbers]})


def test_check_extracts_claims_and_verifies_in_batches():
    responses = [claims_json(BATCH + 2),
                 grounded(verdicts_json(range(1, BATCH + 1))),
                 grounded(json.dumps({"results": [{"n": 1, "verdict": "contradicted", "correction": "9월 24일"},
                                                  {"n": 2, "verdict": "weird"}]}))]
    gemini, client = fake_gemini(responses)
    report = FactChecker(gemini).check(parse_post(post_json()), make_sheet(), "2026-09-29")
    assert len(report.verdicts) == BATCH + 2  # 중복 주장 제거
    assert report.count("supported") == BATCH and report.count("contradicted") == 1
    assert report.verdicts[-1].verdict == "unverifiable"  # 알 수 없는 판정은 확인 불가로
    assert not report.passed
    assert report.summary_line() == f"주장 {BATCH + 2}개 중 {BATCH}개 확인 · 틀림 1 · 확인 불가 1 (검증 1회)"
    assert client.models.calls[1]["config"].tools  # 검증은 구글 검색으로


def test_verify_retries_when_most_verdicts_missing():
    responses = [claims_json(4), grounded(verdicts_json([1])), grounded(verdicts_json([1, 2, 3, 4]))]
    gemini, _ = fake_gemini(responses, models=("a", "b"))
    report = FactChecker(gemini).check(parse_post(post_json()), make_sheet(), "2026-09-29")
    assert report.passed and report.summary_line() == "주장 4개 중 4개 확인 (검증 1회)"


def test_no_claims_is_an_error():
    gemini, _ = fake_gemini([json.dumps({"claims": []})])
    with pytest.raises(ContentError, match="주장"):
        FactChecker(gemini).check(parse_post(post_json()), make_sheet(), "2026-09-29")


def test_long_posts_are_extracted_in_chunks_so_nothing_is_skipped():
    from autopost.fact_check import CHUNK_CHARS, chunk_text

    text = "\n".join(f"{i}번째 줄 " + "가" * 100 for i in range(80))
    chunks = chunk_text(text)
    assert len(chunks) > 1 and all(len(c) <= CHUNK_CHARS + 120 for c in chunks)
    assert "\n".join(chunks) == text  # 빠지는 줄 없음
    assert chunk_text("") == [] and chunk_text("x" * 5000) == ["x" * 5000]


def test_claims_over_limit_fail_closed(monkeypatch):
    from autopost import fact_check

    monkeypatch.setattr(fact_check, "MAX_CLAIMS", 2)
    responses = [claims_json(3), grounded(verdicts_json([1, 2]))]
    gemini, _ = fake_gemini(responses)
    report = FactChecker(gemini).check(parse_post(post_json()), make_sheet(), "2026-09-29")
    assert [v.verdict for v in report.verdicts] == ["supported", "supported", "unverifiable"]
    assert not report.passed and "검증 한도" in report.verdicts[-1].correction


def test_report_markdown_and_issue_export():
    report = FactCheckReport((
        ClaimVerdict("A", "supported", "https://a"),
        ClaimVerdict("B", "contradicted", "https://b", "C 가 맞음"),
    ), rounds=2)
    text = report.markdown("제목")
    assert "✅ 확인 A" in text and "❌ 틀림 B" in text and "바로잡기: C 가 맞음" in text
    assert report.issues[0].as_issue() == {"claim": "B", "verdict": "contradicted", "evidence": "https://b",
                                           "correction": "C 가 맞음"}
    assert not FactCheckReport(()).passed
