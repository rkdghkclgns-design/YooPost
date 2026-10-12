import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from autopost.cloud_drafts import read_json, write_json
from autopost.daily_run import daily_health, parsed
from autopost.draft_progress import build_rows
from autopost.draft_request import with_request
from scripts import cloud_drafts as helper
from test_cloud_drafts import branch_repo  # noqa: F401
from test_codex_fallback import RID

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone(timedelta(hours=9)))
DAY = NOW.strftime("%Y%m%d")


def test_git_utc_z_timestamps_are_supported_on_windows_python_310():
    assert parsed("2026-10-12T23:40:00Z", NOW) == NOW - timedelta(minutes=20)


def commit(tree, paths, at):
    env = {**os.environ, "GIT_AUTHOR_DATE": at.isoformat(), "GIT_COMMITTER_DATE": at.isoformat()}
    for args in (("add", "--", *paths), ("commit", "-qm", "activity"),
                 ("push", "-q", "origin", "HEAD:yoopost-drafts")):
        subprocess.run(["git", *args], cwd=tree, env=env, check=True, capture_output=True)


@pytest.mark.parametrize("idle,running", [(59, True), (60, False), (90, False)])
def test_start_record_does_not_protect_a_stalled_writer_forever(tmp_path, idle, running):
    write_json(tmp_path / f"runs/{DAY}.json", {"started": (NOW - timedelta(minutes=idle)).isoformat()})
    health = daily_health(tmp_path, NOW)
    assert health["running"] is running
    assert health["idle_minutes"] == idle


def test_actual_upload_refreshes_activity_but_manual_and_state_commits_do_not(branch_repo):
    _, _, tree = branch_repo
    start = NOW - timedelta(minutes=100)
    write_json(tree / f"runs/{DAY}.json", {"started": start.isoformat()})
    commit(tree, [f"runs/{DAY}.json"], start)
    write_json(tree / "state/progress.json", {"ts": "new"})
    manual = f"drafts/{DAY}_084000_3"
    write_json(tree / manual / "research.json", with_request({}, RID, "single"))
    commit(tree, ["state/progress.json", manual], NOW - timedelta(minutes=1))
    assert daily_health(tree, NOW)["status"] == "stalled"
    daily = f"drafts/{DAY}_073000_1"
    write_json(tree / daily / "research.json", {"factcheck": {"claims": 1, "confirmed": 1}})
    commit(tree, [daily], NOW - timedelta(minutes=20))
    assert daily_health(tree, NOW)["running"]
    assert daily_health(tree, NOW)["idle_minutes"] == 20


def test_recent_heartbeat_protects_long_research_and_invalid_time_does_not(tmp_path):
    path = tmp_path / f"runs/{DAY}.json"
    run = {"started": (NOW - timedelta(minutes=100)).isoformat(), "category": "AI 보안", "phase": "사실 검증"}
    write_json(path, {**run, "activity_at": (NOW - timedelta(minutes=10)).isoformat()})
    assert daily_health(tmp_path, NOW)["running"]
    assert daily_health(tmp_path, NOW)["category"] == "AI 보안"
    for invalid in ("broken", "2026-10-13T09:00:00", (NOW + timedelta(hours=1)).isoformat()):
        write_json(path, {**run, "activity_at": invalid})
        assert daily_health(tmp_path, NOW)["status"] == "stalled"


def test_shallow_checkout_timestamp_cannot_revive_a_stalled_run(branch_repo, tmp_path):
    remote, _, tree = branch_repo
    start = NOW - timedelta(minutes=100)
    write_json(tree / f"runs/{DAY}.json", {"started": start.isoformat()})
    commit(tree, [f"runs/{DAY}.json"], start)
    write_json(tree / "state/progress.json", {"ts": "fresh checkout"})
    commit(tree, ["state/progress.json"], NOW - timedelta(minutes=1))
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth=1", "--branch=yoopost-drafts", remote.as_uri(), str(shallow)],
                   check=True, capture_output=True)
    assert daily_health(shallow, NOW)["status"] == "stalled"


@pytest.mark.parametrize("action", ["start", "heartbeat", "finish", "push", "skip"])
def test_late_claude_cannot_modify_daily_work_after_codex_takeover(tmp_path, monkeypatch, action):
    monkeypatch.setattr(helper, "TREE", tmp_path)
    write_json(helper.run_file(), {"started": helper.datetime.now(helper.KST).isoformat(),
                                 "fallback": {"provider": "codex"}})
    folder = tmp_path / "drafts" / f"{helper.today()}_073000_2"
    with pytest.raises(SystemExit, match="이어받았습니다"):
        {"start": lambda: helper.start("full"), "heartbeat": lambda: helper.heartbeat("AI 도구", "사실 검증"),
         "finish": helper.finish, "push": lambda: helper.push(folder),
         "skip": lambda: helper.skip("AI 도구", "없음")}[action]()
    assert read_json(helper.run_file())["fallback"]["provider"] == "codex"
    assert not folder.exists()


def test_publish_rechecks_the_owner_after_rebase_receives_a_takeover(branch_repo, monkeypatch):
    remote, seed, tree = branch_repo
    monkeypatch.setattr(helper, "TREE", tree)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=seed, check=True, capture_output=True)
    write_json(seed / f"runs/{helper.today()}.json", {"fallback": {"provider": "codex"}})
    commit(seed, [f"runs/{helper.today()}.json"], NOW)
    write_json(tree / "drafts/late/skip.json", {"reason": "late Claude"})
    with pytest.raises(SystemExit, match="이어받았습니다"):
        helper.publish(["drafts/late/skip.json"], "late", guard=helper.ensure_daily_owner)
    remote_files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "origin/yoopost-drafts"], cwd=tree,
                                  check=True, capture_output=True, text=True).stdout
    assert "drafts/late/skip.json" not in remote_files


def test_daily_board_uses_actual_category_and_stall_or_retry_state():
    rows = build_rows([], {}, None, NOW.timestamp(), writer={"status": "stalled", "reason": "90분 경과"})
    assert rows[0].icon == "⚠️" and not rows[0].finished
    assert not any(row.icon == "✍️" for row in rows)
    rows = build_rows([], {}, None, NOW.timestamp(), writer={"status": "writing", "category": "AI 보안"})
    assert [row.category for row in rows if row.icon == "✍️"] == ["AI 보안"]
    rows = build_rows([], {}, None, NOW.timestamp(), writer={"status": "failed", "category": "AI 보안", "reason": "인증 실패"})
    assert rows[4].icon == "⚠️" and "인증 실패" in rows[4].text
    assert not any(row.icon == "✍️" for row in rows)
    rows = build_rows([], {"AI 보안": "출처 부족"}, None, NOW.timestamp(),
                      writer={"status": "writing", "category": "AI 보안"})
    assert [row.category for row in rows if row.icon == "✍️"] == ["AI 보안"]
    assert not rows[4].finished
