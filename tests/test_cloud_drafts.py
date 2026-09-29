"""클라우드 루틴 흐름 — 원고 브랜치(yoopost-drafts)에 올린 원고를 Actions 가 슬랙 승인 요청으로 보낸다."""
import json
import os
from datetime import datetime, timezone

import pytest

from autopost.claude_draft import DraftError
from autopost.cloud_drafts import (
    NETWORK_HINT, draft_folders, failed_reasons, fingerprint, folder_day, quality_report, read_json, recent_days,
    run_active, summary_text, sync_and_report, sync_drafts, write_json,
)
from autopost.draft_progress import Row, build_rows, scan
from autopost.draft_store import DraftStore, DraftStoreError
from autopost.history import HistoryEntry
from autopost.slack_helper import SlackNotifier
from fakes import FakeWebClient

NOW = datetime(2026, 9, 30, 7, 30, tzinfo=timezone.utc)
OFFICIAL = "https://www.nvidia.com/en-us/data-center/vera-cpu/"
PARAGRAPH = "엔비디아는 **베라 CPU** 를 공개했습니다. " * 40
SENT = {"status": "pending", "slack": {"channel": "CDRAFT", "ts": "1700.1"}}


def good_post(**overrides) -> dict:
    sections = [{"heading": f"{emoji} 소제목 {i}", "blocks": [{"type": "paragraph", "text": PARAGRAPH}]}
                for i, emoji in enumerate(["📰", "⚙️", "💰", "🔍", "🚀"], start=1)]
    sections[0]["blocks"] += [{"type": "info", "title": "핵심", "text": "요점"},
                              {"type": "image", "image_id": "I1"}]
    sections[1]["blocks"] += [{"type": "table", "headers": ["a", "b"], "rows": [["1", "2"]]},
                              {"type": "table", "headers": ["c", "d"], "rows": [["3", "4"]]}]
    data = {"title": "🤖 베라 CPU 총정리", "category": "AI 뉴스 리뷰", "intro": "안녕하세요! **베라** 소식입니다.",
            "summary": ["요약"], "sections": sections,
            "faq": [{"q": f"질문 {i}", "a": "답"} for i in range(4)],
            "closing": "지금까지 베라 CPU 에 대해 알아보았습니다.", "hashtags": ["AI"], "source_ids": ["S1"]}
    data.update(overrides)
    return data


def write_draft(folder, post=None, factcheck=None, official=True, images=True):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "post.json").write_text(json.dumps(post or good_post(), ensure_ascii=False), encoding="utf-8")
    research = {"topic": "NVIDIA Vera CPU 공개", "source_url": OFFICIAL,
                "sources": [{"id": "S1", "title": "Vera", "url": OFFICIAL, "publisher": "NVIDIA", "official": official}],
                "images": [{"id": "I1", "image_url": "", "page_url": OFFICIAL, "caption": "사진"}] if images else [],
                "factcheck": {"claims": 30, "confirmed": 30, "removed": []} if factcheck is None else factcheck}
    (folder / "research.json").write_text(json.dumps(research, ensure_ascii=False), encoding="utf-8")
    return folder


def test_folder_names_days_and_fingerprints(tmp_path):
    assert folder_day("20260930_070512_3") == "20260930" and folder_day("progress_20260930") == ""
    assert recent_days(NOW) == ("20260930", "20260929")
    drafts = tmp_path / "drafts"
    a = write_draft(drafts / "20260930_070512_1")
    write_draft(drafts / "20260920_070512_1")
    (drafts / "notes").mkdir()
    assert draft_folders(drafts, recent_days(NOW)) == [a]
    assert draft_folders(tmp_path / "none", ["20260930"]) == []
    before = fingerprint(a)
    (a / "post.json").write_text("{}", encoding="utf-8")
    assert fingerprint(a) != before
    write_json(tmp_path / "x" / "s.json", {"a": 1})
    assert read_json(tmp_path / "x" / "s.json") == {"a": 1} and read_json(tmp_path / "missing.json") == {}
    (tmp_path / "list.json").write_text("[1]", encoding="utf-8")
    assert read_json(tmp_path / "list.json") == {}


def test_quality_report_passes_a_complete_draft_and_lists_every_gap(tmp_path):
    report = quality_report(write_draft(tmp_path / "ok"), 1000)
    assert report.ok and "이모지 5" in report.stats and "공식 1" in report.stats

    thin = good_post(sections=[{"heading": "배경", "blocks": [{"type": "paragraph", "text": "짧은 글"}]},
                               {"heading": "정리", "blocks": [{"type": "image", "image_id": "I9"}]}],
                     faq=[{"q": "q", "a": "a"}])
    bad = quality_report(write_draft(tmp_path / "bad", thin, factcheck={}, official=False), 1000)
    text = " / ".join(bad.problems)
    for expected in ("최소 1,000자", "이모지 소제목이 0개", "FAQ 가 1개", "표가 0개", "박스", "형광 강조", "공식 출처",
                     "I9", "factcheck"):
        assert expected in text
    assert not bad.ok
    broken = tmp_path / "broken"
    broken.mkdir()
    assert "research.json" in quality_report(broken, 1000).problems[0]


class Store:
    def __init__(self, items=None, broken=()):
        self.items = dict(items or {})
        self.broken = set(broken)

    def find(self, draft_id):
        if draft_id in self.broken:
            raise DraftStoreError("HTTP 500")
        return self.items.get(draft_id)

    def get(self, draft_id):
        if draft_id not in self.items:
            raise DraftStoreError("없음")
        return self.items[draft_id]


def test_sync_sends_only_new_drafts_and_remembers_failures(tmp_path):
    drafts = tmp_path / "drafts"
    new = write_draft(drafts / "20260930_070000_1")
    old = write_draft(drafts / "20260930_071000_2")
    dup = write_draft(drafts / "20260930_072000_3")
    flaky = write_draft(drafts / "20260930_073000_4")
    skipped = drafts / "20260930_074000_7"
    write_json(skipped / "skip.json", {"category": "AI 공모주 분석", "reason": "공모 없음"})
    (drafts / "20260930_075000_5").mkdir()  # 아직 쓰는 중 (파일 없음)
    sent = []

    def send(folder):
        if folder == dup:
            raise DraftError("이미 발행한 주제와 겹칩니다")
        sent.append(folder.name)

    store = Store({old.name: SENT}, broken={flaky.name})
    folders = draft_folders(drafts, ["20260930"])
    outcome, records = sync_drafts(folders, store.find, send, {"gone": {"hash": "x", "reason": "old"}})
    assert outcome.sent == (new.name,) and outcome.already == (old.name,) and outcome.unknown == (flaky.name,)
    assert dict(outcome.new_failures) == {dup.name: "이미 발행한 주제와 겹칩니다"}
    assert records[dup.name]["hash"] == fingerprint(dup) and records[dup.name]["permanent"]
    assert "gone" not in records  # 지금 없는 원고의 기록은 정리

    store.items[new.name] = SENT
    again, records = sync_drafts(folders, store.find, send, records)
    assert again.sent == () and dict(again.new_failures) == {}  # 같은 내용의 실패는 다시 보내지 않음
    write_draft(dup, good_post(title="🤖 고친 제목"))
    sent_before = len(sent)

    def fixed(folder):
        sent.append(folder.name)

    third, records = sync_drafts(folders, store.find, fixed, records)
    assert dup.name in third.sent and len(sent) == sent_before + 1 and dup.name not in records
    assert failed_reasons({"a": {"reason": "r"}}) == {"a": "r"}

    def boom(folder):
        raise RuntimeError("")

    six = [write_draft(drafts / "20260930_080000_6")]
    _, records = sync_drafts(six, Store().find, boom, {})
    assert records["20260930_080000_6"]["reason"] == "RuntimeError"


def test_transient_errors_retry_and_half_sent_drafts_are_resent(tmp_path):
    folder = write_draft(tmp_path / "drafts" / "20260930_070000_1")
    calls = []

    def slack_down(f):
        calls.append(f.name)
        raise RuntimeError("slack 503 token=secret-xyz")

    records = {}
    notices = []
    for _ in range(4):  # 일시적 오류는 3번까지 시도, 알림은 첫 실패 때만
        outcome, records = sync_drafts([folder], Store().find, slack_down, records,
                                       clean=lambda t: t.replace("secret-xyz", "***"))
        notices += list(outcome.new_failures.values())
    assert len(calls) == 3 and notices == ["slack 503 token=***"] and records[folder.name]["attempts"] == 3
    assert not records[folder.name]["permanent"]

    half = Store({folder.name: {"status": "pending"}})  # 보관은 됐지만 슬랙 메시지 위치가 없음 → 다시 보냄
    resent = []
    outcome, _ = sync_drafts([folder], half.find, lambda f: resent.append(f.name), {})
    assert outcome.sent == (folder.name,) and resent == [folder.name]

    link = tmp_path / "drafts" / "20260930_070000_2"
    write_draft(link)
    try:
        (link / "publish.html").symlink_to(tmp_path / "outside.html")
    except OSError:
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없음")
    outcome, records = sync_drafts([link], Store().find, lambda f: resent.append(f.name), {})
    assert "심볼릭 링크" in outcome.new_failures[link.name] and records[link.name]["permanent"]


def test_progress_rows_understand_cloud_drafts(tmp_path):
    drafts = tmp_path / "drafts"
    sent = write_draft(drafts / "20260930_070000_1")
    write_draft(drafts / "20260930_071000_2")
    write_json(drafts / "20260930_072000_7" / "skip.json", {"category": "AI 공모주 분석", "reason": "공모 없음"})
    write_json(drafts / "20260930_072500_6" / "skip.json", {})
    stale = NOW.timestamp() - 3600 * 5
    for folder in drafts.iterdir():
        os.utime(folder, (stale, stale))
    found = scan(drafts, "20260930")
    store = Store({sent.name: {"status": "published", "url": "https://b/1", "slack": {"ts": "1.2"}}})
    rows = build_rows(found, {}, store, NOW.timestamp(), failed={"20260930_071000_2": "이미 발행한 주제"}, active=True)
    icons = [r.icon for r in rows]
    assert icons == ["✅", "⚠️", "✍️", "⬜", "⬜", "⏭️", "⏭️"]  # 보관함에 있으면 보낸 원고 (publish.html 없어도)
    assert "공모 없음" in rows[6].text and "쓸 만한 새 소식 없음" in rows[5].text and "(클라우드)" in rows[2].text
    assert run_active({"started": "t", "finished": ""}) and not run_active({"started": "t", "finished": "u"})
    assert not run_active({})


def test_summary_mentions_waiting_drafts_and_network_hint():
    rows = [Row("AI 언어 모델", "📝", "승인 대기: A", True), Row("AI 도구", "⏭️", "건너뜀 — 없음", True)]
    text = summary_text(rows, "09/30", "limited")
    assert "승인 대기 1편" in text and "📝 AI 언어 모델 — 승인 대기: A" in text and NETWORK_HINT in text
    assert NETWORK_HINT not in summary_text(rows, "09/30", "full")


def test_sync_and_report_runs_the_whole_actions_step(tmp_path):
    root = tmp_path / "branch"
    write_draft(root / "drafts" / "20260930_163000_1")
    client = FakeWebClient()
    slack = SlackNotifier(channel_id="CALERT", client=client)
    store = Store()
    sent = []

    def send(folder):
        sent.append(folder.name)
        store.items[folder.name] = SENT

    clock = lambda: datetime(2026, 9, 30, 16, 40)  # noqa: E731  (KST 기준 오늘)
    entries = [HistoryEntry("2026-09-29", "글", "주제", "AI 도구")]
    write_json(root / "runs" / "20260930.json", {"started": "07:00", "finished": ""})
    outcome = sync_and_report(root, clock, store, send, slack, lambda: entries)
    assert outcome.sent == ("20260930_163000_1",) and sent == ["20260930_163000_1"]
    assert len(client.messages) == 1 and "▰▱▱▱▱▱▱ 1/7" in client.messages[0]["text"]
    assert read_json(root / "state" / "progress_20260930.json")["ts"]
    published = json.loads((root / "state" / "published.json").read_text(encoding="utf-8"))
    assert published[0]["topic"] == "주제"

    write_json(root / "runs" / "20260930.json", {"started": "07:00", "finished": "08:00", "network": "limited"})

    def broken_history():
        raise RuntimeError("blogger down")

    sync_and_report(root, clock, store, send, slack, broken_history)
    summary = client.messages[-1]["text"]
    assert "클라우드 원고 작성 결과" in summary and NETWORK_HINT in summary and len(client.updates) == 1
    sync_and_report(root, clock, store, send, slack, lambda: entries)
    assert sum("작성 결과" in m["text"] for m in client.messages) == 1  # 요약은 한 번만

    def dup(folder):
        raise DraftError("이미 발행한 주제와 겹칩니다")

    write_draft(root / "drafts" / "20260930_164000_2")
    sync_and_report(root, clock, store, dup, slack, lambda: entries)
    assert any("보내지 못했습니다" in m["text"] and "20260930_164000_2" in m["text"] for m in client.messages)
    assert "20260930_164000_2" in read_json(root / "state" / "failures.json")

    quiet = tmp_path / "quiet"
    sync_and_report(quiet, clock, Store(), send, slack, lambda: entries)
    assert not (quiet / "state" / "progress_20260930.json").exists()  # 오늘 실행·원고가 없으면 진행 막대를 올리지 않음


class Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)

    def get(self, url, headers, timeout):
        return self.responses.pop(0)


def test_draft_store_find_distinguishes_missing_from_errors():
    store = DraftStore("https://relay.example", "tok",
                       Session(Response(200, {"a": 1}), Response(200, []), Response(404), Response(503)))
    assert store.find("d1") == {"a": 1} and store.find("d1") == {} and store.find("d1") is None
    with pytest.raises(DraftStoreError, match="확인하지 못했습니다"):
        store.find("d1")


# ------------------------------------------------------------------ 스크립트 (클라우드 도우미 · 작성 지침)
import importlib.util  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"script_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def branch_repo(tmp_path):
    """원고 브랜치만 있는 원격 저장소 + 그 브랜치를 받은 작업 폴더."""
    remote, seed, tree = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "tree"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    seed.mkdir()
    git(seed, "init", "-q", "-b", "yoopost-drafts")
    git(seed, "config", "user.email", "t@example.com")
    git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("drafts", encoding="utf-8")
    git(seed, "add", "README.md")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "push", "-q", str(remote), "yoopost-drafts")
    git(tmp_path, "clone", "-q", "-b", "yoopost-drafts", str(remote), str(tree))
    git(tree, "config", "user.email", "t@example.com")
    git(tree, "config", "user.name", "t")
    return remote, seed, tree


def test_cloud_helper_checks_pushes_skips_and_records_runs(branch_repo, monkeypatch, capsys):
    remote, seed, tree = branch_repo
    helper = load_script("cloud_drafts")
    monkeypatch.setattr(helper, "TREE", tree)
    monkeypatch.setattr(helper, "setup", lambda: tree)
    monkeypatch.setenv("MIN_BODY_CHARS", "1000")
    today = helper.today()
    folder = write_draft(tree / "drafts" / f"{today}_070000_1")
    assert helper.main(["check", str(folder)]) == 0

    # 다른 쪽(Actions)이 먼저 올린 커밋이 있어도 받아서 다시 올린다
    git(seed, "pull", "-q", str(remote), "yoopost-drafts")
    (seed / "state.json").write_text("{}", encoding="utf-8")
    git(seed, "add", "state.json")
    git(seed, "commit", "-q", "-m", "state")
    git(seed, "push", "-q", str(remote), "yoopost-drafts")

    assert helper.main(["push", str(folder)]) == 0
    assert helper.main(["skip", "--category", "AI 공모주 분석", "--reason", "공모 없음"]) == 0
    assert helper.main(["start", "--network", "limited"]) == 0
    assert helper.main(["finish"]) == 0
    log = subprocess.run(["git", "log", "--format=%s", "yoopost-drafts"], cwd=remote, capture_output=True,
                         text=True, encoding="utf-8", check=True).stdout
    assert "draft: " in log and "skip: AI 공모주 분석" in log and "시작" in log and "끝" in log and "state" in log
    run = read_json(tree / "runs" / f"{today}.json")
    assert run["network"] == "limited" and run["finished"]

    assert helper.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "1. AI 언어 모델: 완료" in out and "7. AI 공모주 분석: 완료(건너뜀)" in out and "2. AI 도구: 남음" in out

    bad = write_draft(tree / "drafts" / f"{today}_080000_2", factcheck={})
    assert helper.main(["push", str(bad)]) == 1  # 사실 검증 기록이 없으면 올리지 않음
    with pytest.raises(SystemExit, match="안에 있어야"):
        helper.main(["push", str(write_draft(tree.parent / "elsewhere"))])
    helper.publish(["README.md"], "noop")
    assert "변경 없음" in capsys.readouterr().out


def test_brief_reads_the_published_snapshot_and_branch_drafts(tmp_path, monkeypatch, capsys):
    brief = load_script("claude_brief")
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(brief, "load_dotenv", lambda *a, **k: None)
    published = tmp_path / "published.json"
    write_json(published, [HistoryEntry("2026-09-29", "홀로4 총정리", "Holo4", "AI 언어 모델", "https://h").to_dict()])
    drafts = tmp_path / "drafts"
    write_draft(drafts / f"{datetime.now().strftime('%Y%m%d')}_070000_4")
    write_draft(drafts / "20200101_070000_4", good_post(title="오래된 원고"))
    assert brief.main(["--category", "AI 보안", "--new", "--drafts-dir", str(drafts), "--published", str(published)]) == 0
    out = capsys.readouterr().out
    assert "홀로4 총정리  (주제: Holo4 · https://h)" in out
    assert out.count("NVIDIA Vera CPU 공개") == 1  # 오래된 원고(폴더 이름 날짜 기준)는 빠짐
    assert f"[새 원고 폴더] {drafts}" in out
    assert "읽지 못했습니다" in brief.published_snapshot(tmp_path / "missing.json")[0]
    (tmp_path / "odd").mkdir()
    assert brief._folder_time(tmp_path / "odd") > 0


def test_watchdog_warns_when_the_routine_did_not_run(tmp_path):
    from autopost.cloud_drafts import watchdog

    client = FakeWebClient()
    slack = SlackNotifier(channel_id="CALERT", client=client)
    day = datetime(2026, 9, 30, 9, 30)
    assert watchdog(tmp_path, day, slack) is False and "실행 기록이 없습니다" in client.messages[0]["text"]
    write_json(tmp_path / "runs" / "20260930.json", {"started": "07:00"})
    assert watchdog(tmp_path, day, slack) is False and "끝나지 않았습니다" in client.messages[1]["text"]
    write_json(tmp_path / "runs" / "20260930.json", {"started": "07:00", "finished": "09:10"})
    assert watchdog(tmp_path, day, slack) is True and len(client.messages) == 2
