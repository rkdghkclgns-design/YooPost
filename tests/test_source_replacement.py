"""출처 조회 장애를 주입해 새 주제 작성·독립 재검증·횟수와 시간 제한을 확인한다."""
import pytest

from autopost.cloud_drafts import read_json, write_json
from scripts import codex_cloud


def prepare(tmp_path):
    work = tmp_path / "work"
    write_json(work / "task.json", {"category": "AI 도구", "kind": "all", "topic": ""})
    return work


def draft(isolated, url, title="다른 새 소식", valid=True):
    write_json(isolated / "candidate.json", {"title": title, "topic": title, "source_url": url})
    write_json(isolated / "post.json", {"category": "AI 도구", "title": title})
    write_json(isolated / "research.json", {"topic": title, "source_url": url, "sources": [{"url": url}],
                                           "factcheck": {"claims": 1, "confirmed": 1}})
    write_json(isolated / "verification.json", {"claims": [
        {"claim": "공식 발표 사실", "url": url if valid else "", "confirmed": True}]})


@pytest.mark.parametrize("failed_stage", ["author", "verify", "repair"])
def test_lookup_failure_replaces_topic_and_runs_fresh_verification(tmp_path, monkeypatch, failed_stage):
    work, calls, candidates, proofs = prepare(tmp_path), [], [], []
    def fake(isolated, prompt, minutes=20):
        calls.append(prompt)
        stage = "verify" if "지금은 별도 사실 검증" in prompt else "repair" if "전달 형식을 보정" in prompt else "author"
        if stage == "author":
            candidates.append(isolated)
            assert not any((isolated / name).exists() for name in ("post.json", "verification.json", "skip.json"))
            if len(candidates) == 2:
                rejected = read_json(isolated / "rejected-candidates.json")["candidates"]
                assert rejected[0]["source_url"] == "https://official.example/blocked"
                assert "403" in rejected[0]["reason"]
                assert read_json(isolated / "task.json")["category"] == "AI 도구"
        first = len(candidates) == 1
        draft(isolated, "https://official.example/blocked" if first else "https://official.example/new",
              "막힌 소식" if first else "새 소식", valid=not (first and failed_stage == "repair"))
        if first and stage == failed_stage:
            write_json(isolated / "skip.json", {"category": "AI 도구", "failure_kind": "source_unavailable",
                                                "reason": "공식 원문 재접근 HTTP 403"})
        if stage == "verify":
            proofs.append(len(candidates))
    monkeypatch.setattr(codex_cloud, "run_codex", fake)
    assert codex_cloud.generate(work) == 0
    assert len(candidates) == 2 and candidates[0] != candidates[1]
    assert proofs[-1] == 2
    assert read_json(work / "result.json") == {"status": "draft", "source_replacements": 1}
    assert read_json(work / "research.json")["source_url"] == "https://official.example/new"
    assert read_json(work / "verification.json")["claims"][0]["url"].endswith("/new")
    assert len(read_json(work / "generation-report.json")["rejected_candidates"]) == 1


def test_all_sources_blocked_is_bounded_and_never_exports_unverified_draft(tmp_path, monkeypatch):
    work, calls = prepare(tmp_path), []
    write_json(work / "post.json", {"title": "지난 실행에서 남은 원고"})
    write_json(work / "verification.json", {"claims": ["지난 검증"]})
    def fake(isolated, prompt, minutes=20):
        calls.append(prompt)
        assert len(read_json(isolated / "rejected-candidates.json")["candidates"]) == len(calls) - 1
        draft(isolated, f"https://official.example/blocked-{len(calls)}")
        write_json(isolated / "skip.json", {"category": "AI 도구", "reason": "웹 도구 실패·HTTP 403"})
    monkeypatch.setattr(codex_cloud, "run_codex", fake)
    assert codex_cloud.generate(work) == 0
    assert len(calls) == 3
    assert read_json(work / "result.json")["status"] == "skip"
    assert "재작성 2회" in read_json(work / "result.json")["reason"]
    assert not (work / "post.json").exists() and not (work / "verification.json").exists()
    assert len(read_json(work / "generation-report.json")["rejected_candidates"]) == 3


@pytest.mark.parametrize("kind,reason", [("no_topic", "새 이슈 없음"), ("verification_failed", "접근 실패 주장을 삭제했으나 다른 사실 검증 실패")])
def test_other_skip_reasons_do_not_trigger_source_replacement(tmp_path, monkeypatch, kind, reason):
    work, calls = prepare(tmp_path), []
    def fake(isolated, prompt, minutes=20):
        calls.append(prompt)
        write_json(isolated / "skip.json", {"category": "AI 도구", "failure_kind": kind, "reason": reason})
    monkeypatch.setattr(codex_cloud, "run_codex", fake)
    assert codex_cloud.generate(work) == 0
    assert len(calls) == 1
    assert read_json(work / "result.json")["failure_kind"] == kind


def test_auth_or_quota_failure_is_not_mistaken_for_a_source_lookup(tmp_path, monkeypatch):
    work, calls = prepare(tmp_path), []
    def fake(isolated, prompt, minutes=20):
        calls.append(prompt)
        raise RuntimeError("Codex 실행 실패 — 인증 만료·사용량 한도")
    monkeypatch.setattr(codex_cloud, "run_codex", fake)
    with pytest.raises(RuntimeError, match="인증 만료"):
        codex_cloud.generate(work)
    assert len(calls) == 1 and not (work / "result.json").exists()


def test_writer_cannot_reuse_the_blocked_announcement_after_verification(tmp_path, monkeypatch):
    work, authors = prepare(tmp_path), []
    def fake(isolated, prompt, minutes=20):
        if "원고를 조사·작성" in prompt:
            authors.append(isolated)
        number = len(authors)
        draft(isolated, "https://official.example/blocked" if number < 3 else "https://official.example/new",
              "같은 막힌 소식" if number < 3 else "새 소식")
        if number == 1:
            write_json(isolated / "skip.json", {"category": "AI 도구", "failure_kind": "source_unavailable", "reason": "HTTP 403"})
    monkeypatch.setattr(codex_cloud, "run_codex", fake)
    assert codex_cloud.generate(work) == 0
    assert len(authors) == 3
    assert read_json(work / "result.json")["source_replacements"] == 2
    assert read_json(work / "post.json")["title"] == "새 소식"


def test_time_budget_stops_replacement_and_keeps_the_failure_reason(tmp_path, monkeypatch):
    work, calls, elapsed = prepare(tmp_path), [], [0]
    monkeypatch.setattr(codex_cloud.time, "monotonic", lambda: elapsed[0])
    def fake(isolated, prompt, minutes=20):
        calls.append(prompt)
        elapsed[0] = codex_cloud.GENERATION_SECONDS - 60
        write_json(isolated / "skip.json", {"category": "AI 도구", "failure_kind": "source_unavailable", "reason": "HTTP 403"})
    monkeypatch.setattr(codex_cloud, "run_codex", fake)
    assert codex_cloud.generate(work) == 0
    assert len(calls) == 1 and "403" in read_json(work / "result.json")["reason"]


def test_replacement_does_not_change_an_explicit_user_topic(tmp_path, monkeypatch):
    work, authors = prepare(tmp_path), []
    write_json(work / "task.json", {"category": "AI 도구", "kind": "single", "topic": "지정한 제품"})
    def fake(isolated, prompt, minutes=20):
        assert read_json(isolated / "task.json")["topic"] == "지정한 제품"
        if "원고를 조사·작성" in prompt:
            authors.append(isolated)
            assert "주제 범위 안" in prompt
        draft(isolated, f"https://official.example/{len(authors)}", "지정한 제품의 새 관점")
        if len(authors) == 1:
            write_json(isolated / "skip.json", {"category": "AI 도구", "reason": "원문 조회 실패"})
    monkeypatch.setattr(codex_cloud, "run_codex", fake)
    assert codex_cloud.generate(work) == 0
    assert read_json(work / "result.json")["status"] == "draft" and len(authors) == 2


def test_unspecified_single_keeps_the_first_selected_category(tmp_path, monkeypatch):
    work, authors = prepare(tmp_path), []
    write_json(work / "task.json", {"category": "", "kind": "single", "topic": "지정한 제품"})
    def fake(isolated, prompt, minutes=20):
        if "원고를 조사·작성" in prompt:
            authors.append(isolated)
        if len(authors) == 1:
            write_json(isolated / "skip.json", {"category": "AI 도구", "reason": "원문 조회 실패"})
        else:
            assert read_json(isolated / "task.json")["category"] == "AI 도구"
            draft(isolated, "https://official.example/new")
    monkeypatch.setattr(codex_cloud, "run_codex", fake)
    assert codex_cloud.generate(work) == 0
    assert read_json(work / "post.json")["category"] == "AI 도구"


@pytest.mark.parametrize("kind", ["no_topic", "verification_failed"])
def test_daily_unsuitable_candidate_is_replaced_and_history_is_reused(tmp_path, monkeypatch, kind):
    work, authors = prepare(tmp_path), []
    write_json(work / "task.json", {"category":"AI 도구", "kind":"daily", "request":"", "topic":""})
    old = {"title":"이전 실패", "topic":"이전 실패", "source_url":"https://official.example/old", "reason":"검증 실패"}
    write_json(work / "retry-history.json", {"candidates":[old]})
    def fake(isolated, prompt, minutes=20):
        if "원고를 조사·작성" in prompt:
            authors.append(isolated)
            assert read_json(isolated / "rejected-candidates.json")["candidates"][0] == old
        if len(authors) == 1:
            draft(isolated,"https://official.example/rejected", "조건 불충족 후보")
            write_json(isolated / "skip.json", {"category":"AI 도구", "failure_kind":kind,"reason":"조건 부족"})
        else:
            draft(isolated,"https://official.example/new", "적격 후보")
    monkeypatch.setattr(codex_cloud,"run_codex",fake)
    assert codex_cloud.generate(work) == 0
    assert len(authors) == 2
    assert read_json(work / "result.json")["status"] == "draft"
    assert len(read_json(work / "generation-report.json")["rejected_candidates"]) == 2
    assert read_json(work / "verification.json")["claims"][0]["url"].endswith("/new")


def test_history_contains_only_candidate_fields_and_masks_tokens():
    cleaned = codex_cloud.clean_candidates([{"title":"후보", "reason":"token=ghp_abcdefghi", "extra":"secret"}])
    assert "extra" not in cleaned[0] and "ghp_" not in cleaned[0]["reason"]
