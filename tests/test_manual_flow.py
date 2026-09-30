"""슬랙 수동 지시 원고 — 원고 표시 · 매일 빈칸 계산에서 제외 · 지시 스레드 알림 · 클라우드 도우미 명령 · 발행 결과."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from autopost.claude_draft import DraftError, build_draft_blocks, send_for_approval
from autopost.cloud_drafts import missing_categories, read_json, sync_and_report, write_json
from autopost.draft_progress import scan
from autopost.draft_request import (
    manual_thread, new_record, read_request, request_id_of, request_of, request_path, with_request,
)
from autopost.history import HistoryEntry
from autopost.publish_draft import publish_draft
from autopost.request_report import report_requests
from autopost.slack_helper import SlackNotifier
from fakes import FakeBlogger, FakeWebClient
from test_claude_flow import IMAGE, MemoryStore, preparer
from test_claude_flow import write_draft as write_local_draft
from test_cloud_drafts import SENT, Store, branch_repo, git, load_script, write_draft  # noqa: F401  (fixture)

KST = timezone(timedelta(hours=9))
MANUAL = "C0MANUAL01"
NOW = datetime(2026, 9, 30, 16, 40, tzinfo=KST)
TS = f"{int(NOW.timestamp()) - 600}.000100"  # 지시 메시지 ts (10분 전)
RID = f"{MANUAL}-{TS.replace('.', '')}"


def mark(folder, rid=RID, kind="single"):
    research = folder / "research.json"
    write_json(research, with_request(read_json(research), rid, kind))
    return folder


# ------------------------------------------------------------------ 표시 (draft_request)
def test_request_marker_helpers(tmp_path):
    folder = mark(write_draft(tmp_path / "drafts" / "20260930_163000_2"))
    assert request_of(folder) == {"id": RID, "kind": "single"} and request_id_of(folder) == RID
    daily = write_draft(tmp_path / "drafts" / "20260930_070000_2")
    assert request_of(daily) == {} and request_id_of(daily) == ""
    forged = write_draft(tmp_path / "drafts" / "20260930_070000_3")
    write_json(forged / "research.json", {**read_json(forged / "research.json"), "request": {"id": "../../x"}})
    assert request_of(forged) == {}  # 형식이 틀린 표시는 매일 원고로 본다
    skip = tmp_path / "drafts" / "20260930_163100_7"
    write_json(skip / "skip.json", with_request({"category": "AI 공모주 분석", "reason": "DART 차단"}, RID, "all"))
    assert request_of(skip) == {"id": RID, "kind": "all"}
    with pytest.raises(ValueError):
        with_request({}, "C0X-1", "single")
    with pytest.raises(ValueError):
        with_request({}, RID, "many")
    with pytest.raises(ValueError):
        request_path(tmp_path, "../../etc/passwd")
    assert read_request(tmp_path, "../x") == {}
    assert manual_thread(RID, MANUAL) == (MANUAL, TS)
    assert manual_thread(RID, "C0OTHER999") is None and manual_thread(RID, "") is None
    record = new_record(RID, "all", "AI 도구", "무시되는 주제", "U1", NOW.isoformat())
    assert (record["category"], record["topic"], record["started"]) == ("", "", "")
    with pytest.raises(ValueError):
        new_record(RID, "help", "", "", "U1", "")


# ------------------------------------------------------------------ 매일 원고와 분리
def test_manual_drafts_do_not_fill_or_show_on_the_daily_board(tmp_path):
    drafts = tmp_path / "drafts"
    mark(write_draft(drafts / "20260930_060000_1"))  # 07시 전 수동 지시 원고
    gaps = missing_categories(drafts, "20260930", ("AI 언어 모델", "AI 도구"))
    assert [g.category for g in gaps] == ["AI 언어 모델", "AI 도구"]  # 매일 원고로 치지 않는다
    assert missing_categories(drafts, "20260930", ("AI 언어 모델", "AI 도구"), request=RID) == \
        missing_categories(drafts, "20260930", ("AI 언어 모델", "AI 도구"), request=RID, days=["20260930"])
    assert [g.category for g in missing_categories(drafts, "20260930", ("AI 언어 모델", "AI 도구"), request=RID)] == \
        ["AI 도구"]
    assert scan(drafts, "20260930") == []


# ------------------------------------------------------------------ 지시 스레드 알림 (request_report)
def sync(root, store, send, client):
    slack = SlackNotifier(channel_id="CALERT", client=client)
    return sync_and_report(root, lambda: NOW, store, send, slack, lambda: [], manual_channel=MANUAL,
                           draft_channel="CDRAFT")


def test_thread_replies_for_arrival_skip_failure_and_finish(tmp_path):
    root = tmp_path / "branch"
    arrived = mark(write_draft(root / "drafts" / "20260930_163000_2"), kind="all")
    write_json(root / "drafts" / "20260930_163500_7" / "skip.json",
               with_request({"category": "AI 공모주 분석", "reason": "DART <차단>"}, RID, "all"))
    broken = mark(write_draft(root / "drafts" / "20260930_163200_5"), kind="all")
    store, client = Store(), FakeWebClient()

    def send(folder):
        if folder == broken:
            raise DraftError("이미 발행한 주제와 겹칩니다")
        store.items[folder.name] = SENT
        return "1790000000.000001"

    write_json(request_path(root, RID), {**new_record(RID, "all", "", "", "U1", NOW.isoformat()), "started": NOW.isoformat()})
    sync(root, store, send, client)
    thread = [m for m in client.messages if m.get("channel") == MANUAL]
    assert all(m["thread_ts"] == TS for m in thread)
    texts = "\n".join(m["text"] for m in thread)
    assert "원고 도착" in texts and "AI 도구" in texts and "yoopost.slack.com/archives/CDRAFT/p1790000000000001" in texts
    assert "DART &lt;차단&gt;" in texts and "AI 공모주 분석" in texts  # 이유는 이스케이프
    assert "보내지 못했습니다" in texts and "AI 보안" in texts
    assert not (root / "state" / "progress_20260930.json").exists()  # 수동 지시만 있으면 매일 진행 막대는 없다
    assert f"arrived:{arrived.name}" in read_json(root / "state" / "requests.json")[RID]["reported"]

    before = len(client.messages)
    sync(root, store, send, client)
    assert len(client.messages) == before  # 같은 알림은 한 번만

    record = read_json(request_path(root, RID))
    write_json(request_path(root, RID), {**record, "finished": NOW.isoformat(), "note": "공모주는 DART 차단"})
    sync(root, store, send, client)
    summary = client.messages[-1]
    assert summary["channel"] == MANUAL and summary["thread_ts"] == TS
    assert "끝났습니다" in summary["text"] and "7개 카테고리" in summary["text"]
    assert "AI 도구: 📝" in summary["text"] and "AI 공모주 분석: ⏭️" in summary["text"]
    assert "AI 보안: ⚠️" in summary["text"] and "AI 언어 모델: ❌ 원고 없음" in summary["text"]
    assert "승인 대기 1편" in summary["text"]
    sync(root, store, send, client)
    assert sum("끝났습니다" in m["text"] for m in client.messages) == 1


def test_stale_requests_and_forged_channels(tmp_path):
    root = tmp_path / "branch"
    client, store = FakeWebClient(), Store()
    old_start = (NOW - timedelta(hours=4)).isoformat()
    write_json(request_path(root, RID), {**new_record(RID, "single", "AI 도구", "t", "U1", old_start), "started": old_start})
    forged_rid = f"C0OTHER999-{TS.replace('.', '')}"
    mark(write_draft(root / "drafts" / "20260930_163000_2"), rid=forged_rid)
    write_json(request_path(root, forged_rid), {"finished": NOW.isoformat()})
    sync(root, store, lambda f: "1790000000.000002", client)
    manual = [m for m in client.messages if m.get("channel") == MANUAL]
    assert len(manual) == 1 and "끝나지 않았습니다" in manual[0]["text"]
    assert not [m for m in client.messages if m.get("channel") == "C0OTHER999"]  # 다른 채널에는 쓰지 않는다
    sync(root, store, lambda f: "x", client)
    assert sum("끝나지 않았습니다" in m["text"] for m in client.messages) == 1

    never = f"{MANUAL}-{int(NOW.timestamp()) - 3000}000100"  # 루틴이 시작하지 않은 요청 (기록만 있음)
    write_json(request_path(root, never), new_record(never, "all", "", "", "U1", (NOW - timedelta(minutes=50)).isoformat()))
    fresh = f"{MANUAL}-{int(NOW.timestamp()) - 60}000100"
    write_json(request_path(root, fresh), new_record(fresh, "all", "", "", "U1", (NOW - timedelta(minutes=1)).isoformat()))
    sync(root, store, lambda f: "x", client)
    sync(root, store, lambda f: "x", client)
    not_started = [m for m in client.messages if "시작되지 않았습니다" in m["text"]]
    assert len(not_started) == 1 and not_started[0]["thread_ts"] == f"{int(NOW.timestamp()) - 3000}.000100"

    ancient = f"{MANUAL}-{int((NOW - timedelta(days=5)).timestamp())}000100"
    write_json(root / "state" / "requests.json", {ancient: {"reported": ["x"], "finished": "", "stale": False}})
    state = report_requests(root, [], type("O", (), {"sent": (), "unknown": (), "new_failures": {}, "messages": {}})(),
                            {}, SlackNotifier(client=FakeWebClient(), channel_id="C"), NOW, MANUAL)
    assert ancient not in state  # 오래된 요청 기록은 정리
    assert report_requests(root, [], None, {}, None, NOW, "") == read_json(root / "state" / "requests.json")


# ------------------------------------------------------------------ 승인 카드·발행 결과
def test_approval_card_is_labelled_and_the_request_is_kept_for_publishing(tmp_path):
    prepared = preparer().prepare(write_local_draft(tmp_path, [IMAGE]), "20260930_163000_2")
    client, store = FakeWebClient(), MemoryStore()
    send_for_approval(prepared, store, SlackNotifier(channel_id="C1", client=client), "CDRAFT", NOW,
                      request={"id": RID, "kind": "single"})
    assert client.messages[0]["text"].startswith("🧑‍💼 수동 지시")
    assert "수동 지시" in build_draft_blocks(prepared, {"id": RID})[0]["text"]["text"]
    assert "수동 지시" not in build_draft_blocks(prepared)[0]["text"]["text"]
    assert store.items["20260930_163000_2"]["request"] == {"id": RID, "kind": "single"}


def test_publishing_a_manual_draft_answers_in_the_request_thread():
    store = MemoryStore()
    base = {"title": "제목", "html": "<p>본문</p>", "labels": ["AI 도구"], "status": "approved",
            "slack": {"channel": "CDRAFT", "ts": "1700.1"}, "request": {"id": RID, "kind": "single"}}
    store.put("d1", base)
    client = FakeWebClient()
    publish_draft("d1", store, FakeBlogger(), SlackNotifier(channel_id="C1", client=client), "U1", "CPUB",
                  manual_channel=MANUAL)
    manual = [m for m in client.messages if m.get("channel") == MANUAL]
    assert len(manual) == 1 and manual[0]["thread_ts"] == TS and "발행 완료" in manual[0]["text"]
    store.put("d2", {**base, "status": "approved", "request": {"id": f"C0OTHER999-{TS.replace('.', '')}"}})
    client = FakeWebClient()
    publish_draft("d2", store, FakeBlogger(), SlackNotifier(channel_id="C1", client=client), "U1", "", manual_channel=MANUAL)
    assert not [m for m in client.messages if m.get("channel") in (MANUAL, "C0OTHER999")]


# ------------------------------------------------------------------ 클라우드 도우미 명령 (scripts/cloud_drafts.py)
def test_cloud_helper_request_commands(branch_repo, monkeypatch, capsys):
    remote, seed, tree = branch_repo
    helper = load_script("cloud_drafts")
    monkeypatch.setattr(helper, "TREE", tree)
    monkeypatch.setattr(helper, "setup", lambda: tree)
    monkeypatch.setitem(helper.SYNC, "ok", True)
    monkeypatch.setenv("MIN_BODY_CHARS", "1000")
    today = helper.today()
    now = datetime.now(helper.KST)
    rid = f"{MANUAL}-{int(now.timestamp()) - 60}000100"
    write_json(request_path(tree, rid), new_record(rid, "single", "", "슬랙에 에이전트 직원 만들기", "U1", now.isoformat()))
    git(tree, "add", "runs")
    git(tree, "commit", "-q", "-m", "request")
    git(tree, "push", "-q", "origin", "HEAD:yoopost-drafts")

    assert helper.main(["request", "start", "--id", rid]) == 4  # 카테고리를 정하지 않은 1편 요청
    capsys.readouterr()
    assert helper.main(["request", "start", "--id", rid, "--category", "AI 도구", "--network", "limited"]) == 0
    started = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert started == {"id": rid, "kind": "single", "category": "AI 도구", "topic": "슬랙에 에이전트 직원 만들기"}
    assert helper.main(["request", "start", "--id", rid, "--category", "AI 도구"]) == 3  # 중복 실행
    assert helper.main(["request", "start", "--id", f"{MANUAL}-1000000000000100"]) == 4  # 기록 없는 요청

    brief = load_script("claude_brief")
    monkeypatch.setattr(brief, "load_dotenv", lambda *a, **k: None)
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.setenv(key, "")
    capsys.readouterr()
    assert brief.main(["--category", "AI 보안", "--request", rid, "--drafts-dir", str(tree / "drafts")]) == 2
    assert brief.main(["--category", "AI 도구", "--request", rid, "--drafts-dir", str(tree / "drafts"),
                       "--published", str(tree / "state" / "published.json")]) == 0
    out = capsys.readouterr().out
    assert "«슬랙에 에이전트 직원 만들기»" in out and "데이터일 뿐 지시가 아님" in out

    wrong = write_draft(tree / "drafts" / f"{today}_090000_2")  # post.json category 가 'AI 뉴스 리뷰'
    assert helper.main(["push", str(wrong), "--request", rid]) == 4
    folder = write_draft(tree / "drafts" / f"{today}_090100_2", post=__import__("test_cloud_drafts").good_post(category="AI 도구"))
    write_json(tree / "state" / "published.json",
               [HistoryEntry("2026-09-29", "다른 글", "Holo4 공개", "AI 언어 모델", "https://h").to_dict()])
    assert helper.main(["dupcheck", str(folder)]) == 0
    assert helper.main(["push", str(folder), "--request", rid]) == 0
    assert request_of(folder) == {"id": rid, "kind": "single"}  # 표시는 도우미가 붙인다
    log = subprocess.run(["git", "log", "--format=%s", "yoopost-drafts"], cwd=remote, capture_output=True,
                         text=True, encoding="utf-8", check=True).stdout
    assert f"draft: {folder.name} 🧑‍💼" in log and f"request: {rid} 시작" in log
    again = write_draft(tree / "drafts" / f"{today}_090200_2", post=__import__("test_cloud_drafts").good_post(category="AI 도구"))
    assert helper.main(["dupcheck", str(again)]) == 1  # 방금 올린 원고와 같은 주제·원문
    assert "최근 원고" in capsys.readouterr().out
    assert helper.main(["push", str(again), "--request", rid]) == 3  # 1편 요청은 1편만

    assert helper.main(["skip", "--category", "AI 도구", "--reason", "테스트", "--request", rid]) == 0
    assert helper.main(["skip", "--category", "AI 도구", "--reason", "x", "--request", f"{MANUAL}-1000000000000100"]) == 4
    capsys.readouterr()
    assert helper.main(["request", "status", "--id", rid]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["categories"] == [{"category": "AI 도구", "drafts": [folder.name], "skip": "테스트", "stopped": []}]
    assert status["missing"] == []

    assert helper.main(["status"]) == 0
    assert "2. AI 도구: 남음" in capsys.readouterr().out  # 수동 지시 원고는 매일 원고로 치지 않는다

    assert helper.main(["request", "finish", "--id", rid, "--note", "끝"]) == 0
    assert read_request(tree, rid)["finished"]
    assert helper.main(["push", str(again), "--request", rid]) == 4  # 끝난 요청에는 더 올리지 않는다

    stale = f"{MANUAL}-{int(now.timestamp()) - 30}000100"
    write_json(request_path(tree, stale),
               new_record(stale, "all", "", "", "U1", (now - timedelta(hours=7)).isoformat()))
    assert helper.main(["request", "start", "--id", stale]) == 3  # 오래된 요청은 시작하지 않는다
    monkeypatch.setitem(helper.SYNC, "ok", False)
    assert helper.main(["request", "status", "--id", rid]) == 2


def test_a_failure_notice_does_not_hide_the_later_arrival(tmp_path):
    """일시적 오류로 '보내지 못했습니다'를 알린 원고가 재시도로 도착하면 '원고 도착'도 단다."""
    from autopost.draft_store import DraftStoreError

    root = tmp_path / "branch"
    mark(write_draft(root / "drafts" / "20260930_163000_2"))
    store, client, tries = Store(), FakeWebClient(), []

    def flaky(folder):
        tries.append(folder.name)
        if len(tries) == 1:
            raise DraftStoreError("HTTP 503")
        store.items[folder.name] = SENT
        return "1790000000.000003"

    sync(root, store, flaky, client)
    sync(root, store, flaky, client)
    texts = [m["text"] for m in client.messages if m.get("channel") == MANUAL]
    assert any("보내지 못했습니다" in t for t in texts) and any("원고 도착" in t for t in texts)


def test_closed_requests_are_not_started_or_warned(tmp_path, branch_repo, monkeypatch, capsys):
    remote, seed, tree = branch_repo
    helper = load_script("cloud_drafts")
    monkeypatch.setattr(helper, "TREE", tree)
    monkeypatch.setattr(helper, "setup", lambda: tree)
    monkeypatch.setitem(helper.SYNC, "ok", True)
    now = datetime.now(helper.KST)
    rid = f"{MANUAL}-{int(now.timestamp()) - 60}000100"
    closed = {**new_record(rid, "all", "", "", "U1", (now - timedelta(hours=1)).isoformat()), "fire_error": "HTTP 429"}
    write_json(request_path(tree, rid), closed)
    assert helper.main(["request", "start", "--id", rid]) == 3  # 루틴 실행에 실패해 닫힌 요청
    client = FakeWebClient()
    report_requests(tree, [], type("O", (), {"sent": (), "unknown": (), "new_failures": {}, "messages": {}})(), {},
                    SlackNotifier(client=client, channel_id="C"), now, MANUAL)
    assert client.messages == []  # 그 자리에서 이미 알렸으니 '시작되지 않았습니다'를 다시 달지 않는다


def test_push_keeps_a_broken_research_file_and_parked_drafts_do_not_block(branch_repo, monkeypatch, capsys):
    from test_cloud_drafts import good_post
    from autopost.cloud_drafts import fingerprint

    remote, seed, tree = branch_repo
    helper = load_script("cloud_drafts")
    monkeypatch.setattr(helper, "TREE", tree)
    monkeypatch.setattr(helper, "setup", lambda: tree)
    monkeypatch.setitem(helper.SYNC, "ok", True)
    monkeypatch.setenv("MIN_BODY_CHARS", "1000")
    today, now = helper.today(), datetime.now(helper.KST)
    rid = f"{MANUAL}-{int(now.timestamp()) - 60}000100"
    write_json(request_path(tree, rid), new_record(rid, "single", "AI 도구", "t", "U1", now.isoformat()))
    assert helper.main(["request", "start", "--id", rid, "--category", "AI 도구"]) == 0

    broken = write_draft(tree / "drafts" / f"{today}_100000_2", post=good_post(category="AI 도구"))
    (broken / "research.json").write_text("{깨짐", encoding="utf-8")
    assert helper.main(["push", str(broken), "--request", rid]) == 1
    assert (broken / "research.json").read_text(encoding="utf-8") == "{깨짐"  # 덮어쓰지 않는다

    first = write_draft(tree / "drafts" / f"{today}_100100_2", post=good_post(category="AI 도구"))
    assert helper.main(["push", str(first), "--request", rid]) == 0
    # Actions 가 이 원고를 중복으로 거절해 멈춤(parked) → 대체 원고는 막지 않고, 상태에도 '멈춤'으로 보인다
    write_json(tree / "state" / "failures.json",
               {first.name: {"hash": fingerprint(first), "reason": "이미 발행한 주제와 겹칩니다", "attempts": 1, "permanent": True}})
    second = write_draft(tree / "drafts" / f"{today}_100200_2", post=good_post(category="AI 도구"))
    assert helper.main(["dupcheck", str(second)]) == 0
    capsys.readouterr()
    assert helper.main(["request", "status", "--id", rid]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["missing"] == ["AI 도구"] and "겹칩니다" in status["categories"][0]["stopped"][0]
    assert helper.main(["push", str(second), "--request", rid]) == 0
