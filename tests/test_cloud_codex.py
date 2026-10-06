import json
import subprocess
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

from autopost.cloud_codex import choose_task, cloud_lease, reserve, retry_ready, settle, task_key
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


def test_lease_expiry_and_owner_prevent_other_jobs_releasing_it(tmp_path):
    state = reserve({}, task(), "job1", NOW)
    write_json(tmp_path / "state/codex.json", state)
    assert cloud_lease(tmp_path, NOW)["owner"] == "job1"
    assert not cloud_lease(tmp_path, NOW + timedelta(hours=2))
    with pytest.raises(ValueError):
        settle(state, task(), "job2", "done")
    ended = settle(state, task(), "job1", "failed", "제한")
    assert not ended["lease"] and ended["attempts"][task_key(task())]["status"] == "failed"


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
        assert records[-1]["fire_error"] == "HTTP 429"
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
    assert not dispatched


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


def test_all_request_continues_after_one_verified_draft(branch_repo, monkeypatch, tmp_path):
    _, _, tree = branch_repo
    helper = codex_cloud.helper
    monkeypatch.setattr(helper, "TREE", tree)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("MIN_BODY_CHARS", "1000")
    monkeypatch.setattr(codex_cloud, "notice", lambda task, text: None)
    outputs = []
    monkeypatch.setattr(codex_cloud, "output", lambda name, value: outputs.append((name, value)))
    live = codex_cloud.datetime.now(codex_cloud.KST)
    selected = task(day=live.strftime("%Y%m%d"), request=RID, kind="all", owner="123-1")
    write_json(request_path(tree, RID), record(kind="all", category="", created=live.isoformat(), started=live.isoformat()))
    write_json(tree / "state/codex.json", reserve({}, selected, "123-1", live))
    helper.publish(["state/codex.json", str(request_path(tree, RID).relative_to(tree))], "manual lease")
    work = tmp_path / "result"
    write_draft(work, post=good_post(category="AI 도구"), published=live.strftime("%Y-%m-%d"))
    write_json(work / "result.json", {"status": "draft"})
    write_json(work / "verification.json", {"claims": [
        {"claim": f"공식 발표 {i}", "url": "https://nvidia.com/vera", "confirmed": True} for i in range(30)]})
    assert codex_cloud.complete(work) == 0
    assert outputs == [("continue", "false"), ("continue", "true")]
    assert not read_json(request_path(tree, RID))["finished"]
    assert not read_json(tree / "state/codex.json")["lease"]


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
