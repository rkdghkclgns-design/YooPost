import json
import subprocess
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from autopost.cloud_codex import choose_task, cloud_lease, recover_terminal_lease, reserve, retry_ready, settle, task_key, validate_primary_source, validate_verification
from autopost.cloud_drafts import read_json, write_json
from autopost.codex_fallback import fallback_requests, takeover_reason
from autopost.draft_request import request_path, with_request
from autopost.manual_request import FireResult, handle_request
from autopost.request_board import board_rows
from autopost.slack_helper import SlackNotifier
from fakes import FakeWebClient
from scripts import codex_auth, codex_cloud
from test_codex_fallback import NOW, RID, record
from test_cloud_drafts import branch_repo, good_post, write_draft  # noqa: F401


def task(**changes):
    return {"day": NOW.strftime("%Y%m%d"), "request": "", "kind": "daily", "category": "AI 도구", **changes}


def test_cloud_selector_prioritises_manual_work_and_respects_wait_and_retry():
    status = {"synced": True, "locked_by": "", "cloud_pending": True, "cloud_running": False,
              "daily": [{"category": "AI 도구", "reason": "원고 없음"}], "requests": []}
    assert not choose_task(status, {}, NOW)
    status["requests"] = [{"id": RID, "kind": "all", "missing": ["AI 도구", "AI 보안"], "reason": "429"}]
    selected = choose_task(status, {}, NOW)
    assert selected["request"] == RID and selected["category"] == "AI 도구"
    state = reserve({}, selected, "owner", NOW)
    assert choose_task(status, state, NOW)["category"] == "AI 보안"
    assert not retry_ready(state, selected, NOW + timedelta(hours=3))
    assert retry_ready(state, selected, NOW + timedelta(hours=4))
    state = reserve(state, selected, "owner", NOW + timedelta(hours=4))
    assert not retry_ready(state, selected, NOW + timedelta(hours=9))
    status["locked_by"] = "codex-cloud"
    assert not choose_task(status, {}, NOW)
    assert not choose_task({**status, "locked_by": "", "synced": False}, {}, NOW)


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("request_count", [1, 2])
def test_plan_finishes_old_requests_then_starts_daily_without_another_schedule(tmp_path, monkeypatch, blocked, request_count):
    helper = codex_cloud.helper
    monkeypatch.setattr(helper, "TREE", tmp_path / "tree")
    monkeypatch.setattr(codex_cloud, "sync", lambda: None)
    monkeypatch.setattr(codex_cloud, "recover_previous_run", lambda state: state)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("CODEX_AUTH_READY", "true")
    pending = [f"request-{n}" for n in range(request_count)]
    finished = []
    def current_status():
        return {"synced": True, "requests": [
            {"id": rid, "kind": "all", "finish_only": True} for rid in pending],
            "cloud_running": blocked, "daily": [{"category": "AI 도구", "reason": "원고 없음"}]}
    def finish(rid, note):
        finished.append(rid)
        pending.remove(rid)
        return 0
    monkeypatch.setattr(codex_cloud, "status", current_status)
    monkeypatch.setattr(helper, "request_finish", finish)
    monkeypatch.setattr(codex_cloud, "save_state", lambda state, *args: write_json(helper.TREE / "state/codex.json", state))
    monkeypatch.setattr(codex_cloud.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="brief"))
    monkeypatch.setattr(codex_cloud, "notice", lambda *args: None)
    outputs = []
    monkeypatch.setattr(codex_cloud, "output", lambda name, value: outputs.append((name, value)))
    work = tmp_path / "work"
    assert codex_cloud.plan(work) == 0
    assert len(finished) == request_count and not pending
    assert (("work", "true") in outputs) is not blocked
    if not blocked:
        selected = read_json(work / "task.json")
        assert selected["request"] == "" and selected["category"] == "AI 도구"
        assert read_json(helper.TREE / "state/codex.json")["lease"]["task"] == selected
        run = read_json(helper.TREE / f"runs/{selected['day']}.json")
        assert run["fallback"]["provider"] == "codex" and run["fallback"]["category"] == "AI 도구"
    else:
        assert not read_json(helper.TREE / "state/codex.json").get("lease")


@pytest.mark.parametrize("finish_code", [0, 1])
def test_plan_does_not_reserve_work_when_request_closure_fails_to_persist(tmp_path, monkeypatch, finish_code):
    monkeypatch.setattr(codex_cloud.helper, "TREE", tmp_path)
    monkeypatch.setattr(codex_cloud, "sync", lambda: None)
    monkeypatch.setattr(codex_cloud, "recover_previous_run", lambda state: state)
    monkeypatch.setattr(codex_cloud, "status", lambda: {"synced": True, "requests": [
        {"id": RID, "finish_only": True}]})
    monkeypatch.setattr(codex_cloud.helper, "request_finish", lambda *args: finish_code)
    outputs = []
    monkeypatch.setattr(codex_cloud, "output", lambda name, value: outputs.append((name, value)))
    if finish_code:
        assert codex_cloud.plan(tmp_path / "work") == finish_code
    else:
        with pytest.raises(RuntimeError, match="종료 상태가 갱신되지"):
            codex_cloud.plan(tmp_path / "work")
    assert outputs == [("work", "false")]
    assert not read_json(tmp_path / "state/codex.json").get("lease")


def test_lease_expiry_and_owner_prevent_other_jobs_releasing_it(tmp_path):
    state = reserve({}, task(), "job1", NOW)
    write_json(tmp_path / "state/codex.json", state)
    assert cloud_lease(tmp_path, NOW)["owner"] == "job1"
    assert not cloud_lease(tmp_path, NOW + timedelta(hours=2))
    with pytest.raises(ValueError):
        settle(state, task(), "job2", "done")
    ended = settle(state, task(), "job1", "failed", "제한")
    assert not ended["lease"] and ended["attempts"][task_key(task())]["status"] == "failed"


def test_terminal_unstarted_queue_refunds_only_its_reserved_attempt():
    selected = task(request=RID)
    state = reserve({}, selected, "123-1", NOW)
    run = {"id": 123, "status": "completed", "conclusion": "cancelled"}
    jobs = [{"name": "write", "status": "completed", "runner_id": 0}]
    recovered, chosen = recover_terminal_lease(state, run, jobs)
    assert chosen == selected and not recovered["lease"]
    assert recovered["attempts"][task_key(selected)]["count"] == 0
    assert retry_ready(recovered, selected, NOW + timedelta(minutes=1))
    assert state["lease"]  # 입력 state는 바꾸지 않는다.
    assert recover_terminal_lease(recovered, run, jobs) == (recovered, {})


def test_terminal_actual_writer_preserves_attempt_and_active_or_other_owner_is_untouched():
    selected = task()
    state = reserve({}, selected, "123-1", NOW)
    jobs = [{"name": "write", "status": "completed", "runner_id": 1}]
    run = {"id": 123, "status": "completed"}
    recovered, _ = recover_terminal_lease(state, run, jobs)
    assert recovered["attempts"][task_key(selected)]["count"] == 1
    assert not retry_ready(recovered, selected, NOW + timedelta(minutes=1))
    assert recover_terminal_lease(state, {**run, "status": "queued"}, jobs) == (state, {})
    assert recover_terminal_lease(state, {**run, "id": 999}, jobs) == (state, {})


def test_all_requests_continue_after_partial_success_and_skip_without_rewriting(tmp_path):
    first = NOW - timedelta(hours=7)
    requested = first - timedelta(minutes=30)
    fb = {"provider": "codex", "mode": "cloud", "status": "waiting", "first_taken_over": first.isoformat()}
    r = record(kind="all", category="", created=requested.isoformat(), started=first.isoformat(), fallback=fb)
    assert takeover_reason(r, NOW)
    assert not takeover_reason(r, NOW + timedelta(hours=25))
    write_json(request_path(tmp_path, RID), r)
    folder = tmp_path / "drafts/20261006_130000_2"
    write_json(folder / "skip.json", with_request({"category": "AI 도구", "reason": "새 이슈 없음"}, RID, "all"))
    assert "AI 도구" not in fallback_requests(tmp_path, NOW, {folder.name})[0]["missing"]
    assert "Codex 클라우드" in board_rows(r, [], None, {})[0].text


def test_quota_failure_dispatches_only_after_request_was_recorded():
    client, records, dispatched = FakeWebClient(), [], []
    notifier = SlackNotifier(client=client, channel_id="C0MANUAL01")
    def fallback(rid):
        assert records[-1]["fire_error"]
        dispatched.append(rid)
        return True
    args = ("AI 도구 한 편 더 발행해줘", "C0MANUAL01", "1791234000.123456", "U1", "message")
    status = handle_request(*args, manual_channel="C0MANUAL01", notifier=notifier,
        fire=lambda text: FireResult(False, error="HTTP 429"), record=records.append, now=NOW, fallback=fallback)
    assert status == "fallback" and dispatched == [RID]
    assert "Codex 클라우드" in client.messages[-1]["text"]
    dispatched.clear()
    handle_request(*args, manual_channel="C0MANUAL01", notifier=notifier,
        fire=lambda text: FireResult(False, error="토큰이 맞지 않습니다"), record=records.append, now=NOW, fallback=fallback)
    assert dispatched == [RID]  # Claude 인증이 끊겨도 독립 Codex 인증으로 이어받는다.


def test_account_cache_is_encrypted_and_refresh_survives_the_next_runner(tmp_path, monkeypatch):
    home, cache = tmp_path / "home", tmp_path / "auth.enc"
    key = Fernet.generate_key().decode()
    auth = {"auth_mode": "chatgpt", "tokens": {"refresh_token": "seed"}}
    for name, value in {"CODEX_HOME": str(home), "CODEX_AUTH_CACHE_FILE": str(cache),
                        "CODEX_AUTH_CACHE_KEY": key, "CODEX_AUTH_JSON": json.dumps(auth), "OPENAI_API_KEY": ""}.items():
        monkeypatch.setenv(name, value)
    codex_auth.restore()
    auth["tokens"]["refresh_token"] = "refreshed"
    (home / "auth.json").write_text(json.dumps(auth))
    codex_auth.persist()
    assert b"refreshed" not in cache.read_bytes()
    (home / "auth.json").unlink()
    codex_auth.restore()
    assert read_json(home / "auth.json")["tokens"]["refresh_token"] == "refreshed"


def test_agent_has_no_github_slack_google_or_cache_key(monkeypatch):
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "SLACK_BOT_TOKEN", "GOOGLE_REFRESH_TOKEN", "CODEX_AUTH_CACHE_KEY"):
        monkeypatch.setenv(key, "secret")
    monkeypatch.setenv("CODEX_HOME", "dedicated-home")
    assert codex_cloud.agent_environment().get("CODEX_HOME") == "dedicated-home"
    assert not set(codex_cloud.agent_environment()) & {"GH_TOKEN", "GITHUB_TOKEN", "SLACK_BOT_TOKEN", "GOOGLE_REFRESH_TOKEN", "CODEX_AUTH_CACHE_KEY"}


def test_fresh_runner_can_commit_the_takeover_without_a_global_git_identity(branch_repo, monkeypatch, tmp_path):
    remote, _, _ = branch_repo
    tree = tmp_path / "fresh-runner"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-git-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    subprocess.run(["git", "clone", "-q", "--branch", "yoopost-drafts", str(remote), str(tree)], check=True)
    monkeypatch.setattr(codex_cloud.helper, "TREE", tree)
    codex_cloud.sync()
    write_json(tree / "state/codex.json", reserve({}, task(), "runner", NOW))
    codex_cloud.save_state(read_json(tree / "state/codex.json"), "codex: fresh runner takeover")
    author = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=tree,
                            check=True, capture_output=True, text=True).stdout.strip()
    assert author == "yoopost-bot <yoopost-bot@users.noreply.github.com>"
    pushed = subprocess.run(["git", "rev-parse", "origin/yoopost-drafts"], cwd=tree,
                            check=True, capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree,
                          check=True, capture_output=True, text=True).stdout.strip()
    assert pushed == head


def test_last_response_diagnostic_removes_dedicated_login_secrets(tmp_path, monkeypatch):
    auth_home = tmp_path / "dedicated-auth"
    monkeypatch.setenv("CODEX_HOME", str(auth_home))
    write_json(auth_home / "auth.json", {"tokens": {"access_token": "secret-access", "refresh_token": "secret-refresh"},
                                        "OPENAI_API_KEY": "secret-api"})
    source, destination = tmp_path / "reply.txt", tmp_path / "diagnostic.txt"
    source.write_text("tool failed secret-access secret-refresh secret-api", encoding="utf-8")
    codex_cloud.save_diagnostic(source, destination)
    text = destination.read_text(encoding="utf-8")
    assert "tool failed" in text and "secret-" not in text


def test_cloud_board_shows_the_actual_category_and_a_failed_wait_instead_of_writing():
    request = record(kind="all", category="", started=NOW.isoformat(),
                     fallback={"provider": "codex", "mode": "cloud", "status": "writing", "category": "AI 도구"})
    rows = board_rows(request, [], None, {})
    assert [r.category for r in rows if r.icon == "✍️"] == ["AI 도구"]
    request["fallback"].update(status="waiting", last_result="failed")
    request["note"] = "원고 파일 없음"
    rows = board_rows(request, [], None, {})
    assert not any(r.icon == "✍️" for r in rows)
    assert "원고 파일 없음" in next(r.text for r in rows if r.category == "AI 도구")


@pytest.mark.parametrize("replacements", [0, 2])
def test_all_request_continues_after_one_verified_draft(branch_repo, monkeypatch, tmp_path, replacements):
    _, _, tree = branch_repo
    helper = codex_cloud.helper
    monkeypatch.setattr(helper, "TREE", tree)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("MIN_BODY_CHARS", "1000")
    notices = []
    monkeypatch.setattr(codex_cloud, "notice", lambda task, text: notices.append(text))
    outputs = []
    monkeypatch.setattr(codex_cloud, "output", lambda name, value: outputs.append((name, value)))
    live = codex_cloud.datetime.now(codex_cloud.KST)
    selected = task(day=live.strftime("%Y%m%d"), request=RID, kind="all", owner="123-1")
    write_json(request_path(tree, RID), record(kind="all", category="", created=live.isoformat(), started=live.isoformat()))
    write_json(tree / "state/codex.json", reserve({}, selected, "123-1", live))
    helper.publish(["state/codex.json", str(request_path(tree, RID).relative_to(tree))], "manual lease")
    work = tmp_path / "result"
    write_draft(work, post=good_post(category="AI 도구"), published=live.strftime("%Y-%m-%d"))
    write_json(work / "result.json", {"status": "draft", "source_replacements": replacements})
    write_json(work / "verification.json", {"claims": [
        {"claim": f"공식 발표 {i}", "url": "https://nvidia.com/vera", "confirmed": True} for i in range(30)]})
    assert codex_cloud.complete(work) == 0
    assert outputs == [("continue", "false"), ("continue", "true")]
    assert not read_json(request_path(tree, RID))["finished"]
    assert not read_json(tree / "state/codex.json")["lease"]
    assert any("🔄 조회 실패 후 다른 주제로 재작성(2회)" in n for n in notices) is bool(replacements)


def test_complete_publishes_checked_data_and_releases_lease_on_failure(branch_repo, monkeypatch, tmp_path):
    _, _, tree = branch_repo
    helper = codex_cloud.helper
    monkeypatch.setattr(helper, "TREE", tree)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("MIN_BODY_CHARS", "1000")
    notices = []
    monkeypatch.setattr(codex_cloud, "notice", lambda task, text: notices.append(text))
    monkeypatch.setattr(helper, "cloud_running", lambda: False)
    live = codex_cloud.datetime.now(codex_cloud.KST)
    selected = task(day=live.strftime("%Y%m%d"), owner="123-1")
    write_json(tree / "state/codex.json", reserve({}, selected, "123-1", live))
    helper.publish(["state/codex.json"], "lease")
    work = tmp_path / "result"
    write_draft(work, post=good_post(category="AI 도구"), published=live.strftime("%Y-%m-%d"))
    write_json(work / "result.json", {"status": "draft"})
    write_json(work / "verification.json", {"claims": [
        {"claim": f"공식 발표 {i}", "url": "https://nvidia.com/vera", "confirmed": True} for i in range(30)]})
    assert codex_cloud.complete(work) == 0
    state = read_json(tree / "state/codex.json")
    assert not state["lease"] and state["attempts"][task_key(selected)]["status"] == "done"
    assert list(tree.glob("state/codex-verification/*.json"))
    selected = {**selected, "category": "AI 보안"}
    write_json(tree / "state/codex.json", reserve(state, selected, "123-1", live))
    helper.publish(["state/codex.json"], "lease2")
    write_draft(work, post=good_post(category="AI 보안"), published=live.strftime("%Y-%m-%d"))
    write_json(work / "verification.json", {"claims": [{"claim": "하나만 확인", "url": "https://nvidia.com/vera", "confirmed": True}]})
    assert codex_cloud.complete(work) == 1
    state = read_json(tree / "state/codex.json")
    assert "개수" in state["attempts"][task_key(selected)]["note"] and not state["lease"]
    write_json(tree / "state/codex.json", reserve(state, selected, "123-1", live))
    helper.publish(["state/codex.json"], "lease3")
    assert codex_cloud.complete(tmp_path / "absent") == 1
    assert not read_json(tree / "state/codex.json")["lease"]
    assert len(notices) == 3
    run = read_json(helper.run_file())
    assert run["fallback"]["last_result"] == "failed" and "result.json" in run["note"]


@pytest.mark.parametrize("blocked", [False, True])
def test_daily_completion_chains_only_categories_with_remaining_attempts(branch_repo, monkeypatch, tmp_path, blocked):
    _, _, tree = branch_repo
    helper = codex_cloud.helper
    monkeypatch.setattr(helper, "TREE", tree)
    monkeypatch.setattr(helper, "cloud_running", lambda: False)
    monkeypatch.setattr(helper, "cloud_pending", lambda: False)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("MIN_BODY_CHARS", "1000")
    monkeypatch.setattr(codex_cloud, "notice", lambda *args: None)
    outputs = []
    monkeypatch.setattr(codex_cloud, "output", lambda name, value: outputs.append((name, value)))
    live = codex_cloud.datetime.now(codex_cloud.KST)
    selected = task(day=live.strftime("%Y%m%d"), owner="123-1")
    state = reserve({}, selected, "123-1", live)
    if blocked:
        for category in codex_cloud.CATEGORIES:
            if category != selected["category"]:
                state["attempts"][task_key({**selected, "category": category})] = {
                    "at": live.isoformat(), "count": 2, "status": "failed"}
    write_json(tree / "state/codex.json", state)
    helper.publish(["state/codex.json"], "daily lease")
    work = tmp_path / "daily-result"
    write_draft(work, post=good_post(category="AI 도구"), published=live.strftime("%Y-%m-%d"))
    write_json(work / "result.json", {"status": "draft"})
    write_json(work / "verification.json", {"claims": [
        {"claim": f"공식 발표 {i}", "url": "https://nvidia.com/vera", "confirmed": True} for i in range(30)]})
    assert codex_cloud.complete(work) == 0
    assert (("continue", "true") in outputs) is not blocked


@pytest.mark.parametrize("blocked", [False, True])
def test_finished_manual_request_immediately_chains_to_eligible_daily_work(branch_repo, monkeypatch, tmp_path, blocked):
    _, _, tree = branch_repo
    helper = codex_cloud.helper
    monkeypatch.setattr(helper, "TREE", tree)
    monkeypatch.setattr(helper, "cloud_running", lambda: False)
    monkeypatch.setattr(helper, "cloud_pending", lambda: False)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("MIN_BODY_CHARS", "1000")
    monkeypatch.setattr(codex_cloud, "notice", lambda *args: None)
    outputs = []
    monkeypatch.setattr(codex_cloud, "output", lambda name, value: outputs.append((name, value)))
    live = codex_cloud.datetime.now(codex_cloud.KST)
    selected = task(day=live.strftime("%Y%m%d"), request=RID, kind="single", owner="123-1")
    write_json(request_path(tree, RID), record(kind="single", category="AI 도구", created=live.isoformat(), started=live.isoformat()))
    state = reserve({}, selected, "123-1", live)
    if blocked:
        for category in codex_cloud.CATEGORIES:
            state["attempts"][task_key({**selected, "request": "", "category": category})] = {
                "at": live.isoformat(), "count": 2, "status": "failed"}
    write_json(tree / "state/codex.json", state)
    helper.publish(["state/codex.json", str(request_path(tree, RID).relative_to(tree))], "manual lease")
    work = tmp_path / "manual-result"
    write_draft(work, post=good_post(category="AI 도구"), published=live.strftime("%Y-%m-%d"))
    write_json(work / "result.json", {"status": "draft"})
    write_json(work / "verification.json", {"claims": [
        {"claim": f"공식 발표 {i}", "url": "https://nvidia.com/vera", "confirmed": True} for i in range(30)]})
    assert codex_cloud.complete(work) == 0
    assert read_json(request_path(tree, RID))["finished"]
    assert not read_json(tree / "state/codex.json")["lease"]
    assert (("continue", "true") in outputs) is not blocked


def test_missing_morning_run_is_eligible_at_0715_and_started_claude_is_protected(tmp_path, monkeypatch):
    from datetime import datetime
    helper = codex_cloud.helper
    monkeypatch.setattr(helper, "TREE", tmp_path)
    class Clock(datetime):
        moment = NOW.replace(hour=7, minute=14)
        @classmethod
        def now(cls, tz=None):
            return cls.moment
    monkeypatch.setattr(helper, "datetime", Clock)
    assert helper.cloud_pending()
    Clock.moment = NOW.replace(hour=7, minute=15)
    assert not helper.cloud_pending()
    write_json(helper.run_file(), {"started": NOW.replace(hour=7, minute=0).isoformat()})
    assert helper.cloud_running()
    Clock.moment = NOW.replace(hour=10, minute=0)
    assert not helper.cloud_running()


def test_authorized_recovery_preserves_attempt_limit_and_is_consumed_once():
    selected = task()
    state = settle(reserve({}, selected, "owner", NOW), selected, "owner", "failed", "보고서 형식 오류")
    key = task_key(selected)
    recovery = {"after_count": 1, "expires": (NOW + timedelta(minutes=30)).isoformat(), "reason": "사용자 장애 복구 요청"}
    state["retry_authorizations"] = {key: recovery}
    assert retry_ready(state, selected, NOW + timedelta(minutes=5))
    assert not retry_ready(state, selected, NOW + timedelta(minutes=31))
    next_state = reserve(state, selected, "owner2", NOW + timedelta(minutes=5))
    assert next_state["attempts"][key]["count"] == 2 and key not in next_state["retry_authorizations"]
    next_state["retry_authorizations"] = {key: {**recovery, "after_count": 2}}
    assert not retry_ready(next_state, selected, NOW + timedelta(minutes=6))
    state["attempts"][key]["status"] = "writing"
    assert not retry_ready(state, selected, NOW + timedelta(minutes=5))


@pytest.mark.parametrize("claim,fc", [
    ({"claim": "외부 주장", "url": "", "confirmed": True}, {"claims": 1, "confirmed": 1}),
    ({"claim": "외부 주장", "url": "https://official.example/report", "confirmed": "true"}, {"claims": 1, "confirmed": 1}),
    ({"claim": "외부 주장", "url": "https://official.example/report", "confirmed": False}, {"claims": 1, "confirmed": 0}),
    ({"claim": "외부 주장", "url": "https://official.example/report", "confirmed": True}, {"claims": 2, "confirmed": 1}),
])
def test_invalid_evidence_is_rejected_before_and_after_generation(claim, fc):
    with pytest.raises(ValueError):
        validate_verification({"factcheck": fc}, {"claims": [claim]})


@pytest.mark.parametrize("repaired", [True, False])
@pytest.mark.parametrize("defect", ["verification", "primary-source"])
def test_generation_repairs_report_format_once_without_bypassing_validation(tmp_path, monkeypatch, repaired, defect):
    work = tmp_path / "work"
    write_json(work / "task.json", task())
    calls = []
    factual = {"claim": "공식 발표", "url": "https://official.example/report", "confirmed": True}
    process = {"claim": "검증 단계 설명", "url": "", "confirmed": False, "action": "note로 이동"}
    def fake_codex(isolated, prompt, minutes=20):
        calls.append(prompt)
        write_json(isolated / "post.json", {"category": "AI 도구"})
        fixed = len(calls) == 3 and repaired
        proof = [factual] if fixed or defect == "primary-source" else [factual, process]
        url = "https://arxiv.org/abs/2609.40286"
        sources = [{"url": url if fixed or defect == "verification" else "https://arxiv.org/html/2609.40286v1"}]
        write_json(isolated / "research.json", {"source_url": url, "sources": sources,
                                                "factcheck": {"claims": len(proof), "confirmed": 1}})
        write_json(isolated / "verification.json", {"claims": proof, "note": "작업 상태 설명"})
    monkeypatch.setattr(codex_cloud, "run_codex", fake_codex)
    if repaired:
        assert codex_cloud.generate(work) == 0
        assert read_json(work / "verification.json")["claims"] == [factual]
        assert read_json(work / "result.json") == {"status": "draft"}
    else:
        with pytest.raises(ValueError, match="검증 근거" if defect == "verification" else "대표 원문"):
            codex_cloud.generate(work)
        assert not (work / "result.json").exists()
    assert len(calls) == 3 and "지어내지" in calls[-1]


@pytest.mark.parametrize("kind,blocked,continued", [("all", False, True), ("all", True, False), ("single", False, False)])
def test_failed_category_does_not_stop_other_work_or_bypass_retry_limits(branch_repo, monkeypatch, tmp_path, kind, blocked, continued):
    _, _, tree = branch_repo
    monkeypatch.setattr(codex_cloud.helper, "TREE", tree)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(codex_cloud, "notice", lambda task, text: None)
    outputs = []
    monkeypatch.setattr(codex_cloud, "output", lambda name, value: outputs.append((name, value)))
    live = codex_cloud.datetime.now(codex_cloud.KST)
    selected = task(day=live.strftime("%Y%m%d"), request=RID, kind=kind, owner="123-1")
    write_json(request_path(tree, RID), record(kind=kind, category="AI 도구" if kind == "single" else "",
                                            created=live.isoformat(), started=live.isoformat()))
    state = reserve({}, selected, "123-1", live)
    if blocked:
        for category in codex_cloud.CATEGORIES:
            if category != selected["category"]:
                state["attempts"][task_key({**selected, "category": category})] = {
                    "at": live.isoformat(), "count": 2, "status": "failed"}
    write_json(tree / "state/codex.json", state)
    codex_cloud.helper.publish(["state/codex.json", str(request_path(tree, RID).relative_to(tree))], "manual lease")
    work = tmp_path / "failed-result"
    write_json(work / "result.json", {"status": "draft", "category": "AI 도구"})
    write_json(work / "post.json", {"category": "AI 도구"})
    write_json(work / "research.json", {"factcheck": {"claims": 0, "confirmed": 0}})
    write_json(work / "verification.json", {"claims": []})
    assert codex_cloud.complete(work) == 1
    assert (("continue", "true") in outputs) is continued
    ended = read_json(tree / "state/codex.json")
    assert not ended["lease"]
    assert ended["attempts"][task_key(selected)]["count"] == 1
    assert not retry_ready(ended, selected, live + timedelta(minutes=1))


def test_primary_source_requires_the_actual_metadata_page_in_sources():
    abs_url = "https://arxiv.org/abs/2609.40286"
    html_url = "https://arxiv.org/html/2609.40286v1"
    research = {"source_url": abs_url, "sources": [{"url": html_url, "metadata_url": abs_url}]}
    with pytest.raises(ValueError, match="대표 원문"):
        validate_primary_source(research)
    research["sources"].append({"url": abs_url, "published": "2026-09-30", "official": True})
    validate_primary_source(research)
