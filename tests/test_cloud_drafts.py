"""클라우드 루틴 흐름 — 원고 브랜치(yoopost-drafts)에 올린 원고를 Actions 가 슬랙 승인 요청으로 보낸다."""
import json
import os
from datetime import datetime, timezone

import pytest

from autopost.claude_draft import DraftError
from autopost.cloud_drafts import (
    AUTOFILL_HINT, NETWORK_HINT, draft_folders, failed_reasons, fingerprint, folder_day, missing_categories,
    quality_report, read_json, recent_days, run_active, summary_text, sync_and_report, sync_drafts, write_json,
)
from autopost.constants import CATEGORIES
from autopost.draft_progress import Row, build_rows, scan
from autopost.draft_store import DraftStore, DraftStoreError
from autopost.history import HistoryEntry
from autopost.slack_helper import SlackNotifier
from fakes import FakeWebClient

NOW = datetime(2026, 9, 30, 7, 30, tzinfo=timezone.utc)
OFFICIAL = "https://www.nvidia.com/en-us/data-center/vera-cpu/"
PRESS = "https://www.theverge.com/nvidia-vera-cpu"
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


def draft_day(name):
    """원고 폴더 이름의 날짜 (YYYY-MM-DD) — 형식이 아니면 오늘. 출처 발표일을 원고일로 두면 '최신 이슈' 조건을 통과한다."""
    try:
        return datetime.strptime(name[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return datetime.now().strftime("%Y-%m-%d")


def write_draft(folder, post=None, factcheck=None, official=True, images=True, published=None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "post.json").write_text(json.dumps(post or good_post(), ensure_ascii=False), encoding="utf-8")
    day = draft_day(folder.name) if published is None else published
    research = {"topic": "NVIDIA Vera CPU 공개", "source_url": OFFICIAL,
                "sources": [{"id": "S1", "title": "Vera", "url": OFFICIAL, "publisher": "NVIDIA", "official": official,
                             "published": day},
                            {"id": "S2", "title": "Vera 보도", "url": PRESS, "publisher": "The Verge", "published": day}],
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
    assert AUTOFILL_HINT in text  # 건너뛴 카테고리가 있으면 자동 보충 안내
    assert NETWORK_HINT not in summary_text(rows, "09/30", "full")
    assert AUTOFILL_HINT not in summary_text(rows[:1], "09/30", "full")


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
    monkeypatch.setitem(helper.SYNC, "ok", True)
    monkeypatch.setenv("MIN_BODY_CHARS", "1000")
    today = helper.today()
    folder = write_draft(tree / "drafts" / f"{today}_070000_1", post=good_post(category="AI 언어 모델"))
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
    assert "1. AI 언어 모델: 완료" in out and "2. AI 도구: 남음 — 원고 없음" in out
    assert "7. AI 공모주 분석: 남음 — 건너뜀: 공모 없음" in out  # 건너뛴 카테고리는 다시 쓸 대상
    assert helper.main(["missing"]) == 0
    out = capsys.readouterr().out
    assert "1. AI 언어 모델" not in out and "7. AI 공모주 분석 — 건너뜀: 공모 없음" in out
    assert helper.main(["missing", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [g["number"] for g in listed["gaps"]] == [2, 3, 4, 5, 6, 7] and listed["gaps"][-1]["category"] == "AI 공모주 분석"
    assert listed["cloud_running"] is False  # 'finish' 로 끝난 실행
    write_json(tree / "runs" / f"{today}.json", {"started": datetime.now(helper.KST).isoformat(), "finished": ""})
    assert helper.cloud_running() is True  # 방금 시작해 진행 중 → 로컬 보충은 기다린다
    helper.main(["missing"])
    assert "클라우드 루틴 자신이라면" in capsys.readouterr().out
    write_json(tree / "runs" / f"{today}.json", {"started": "2000-01-01T07:00:00+09:00", "finished": ""})
    assert helper.cloud_running() is False  # 오래전에 시작해 멈춘 실행은 기다리지 않는다
    write_json(tree / "runs" / f"{today}.json", {"started": "?", "finished": ""})
    assert helper.cloud_running() is False

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
    for number in range(1, 7):
        write_draft(tmp_path / "drafts" / f"20260930_07000{number}_{number}")
    write_json(tmp_path / "drafts" / "20260930_073000_7" / "skip.json", {"category": "AI 공모주 분석", "reason": "공시 차단"})
    assert watchdog(tmp_path, day, slack) is False  # 끝났어도 빈 카테고리가 있으면 알린다
    gap_note = client.messages[2]["text"]
    assert "원고가 없는 카테고리 1개" in gap_note and "AI 공모주 분석 — 건너뜀: 공시 차단" in gap_note
    assert AUTOFILL_HINT in gap_note
    write_draft(tmp_path / "drafts" / "20260930_120000_7")  # 자동 보충한 원고가 건너뜀을 이긴다
    assert watchdog(tmp_path, day, slack) is True and len(client.messages) == 3


def test_missing_categories_cover_skips_failures_and_absent_drafts(tmp_path):
    from autopost.cloud_drafts import fingerprint as mark

    drafts = tmp_path / "drafts"
    one = write_draft(drafts / "20260930_070000_1")
    two = write_draft(drafts / "20260930_070500_2")  # 원고 문제로 보내지 못함 (영구 실패)
    three = write_draft(drafts / "20260930_071000_3")  # 일시적 실패는 Actions 가 다시 보내므로 빈 카테고리가 아님
    write_draft(drafts / "20260930_071500_4", factcheck={})  # 사실 검증 전 원고는 쓸 수 없음
    write_json(drafts / "20260930_072000_5" / "skip.json", {"category": "AI 보안"})
    write_draft(drafts / "20260929_070000_6")  # 어제 원고는 오늘 것이 아님
    seven = write_draft(drafts / "20260930_073000_7")  # 일시적 실패를 3번 겪어 멈춤 (parked)
    failures = {two.name: {"hash": mark(two), "reason": "이미 발행한 주제", "permanent": True},
                three.name: {"hash": mark(three), "reason": "슬랙 503", "attempts": 1, "permanent": False},
                seven.name: {"hash": mark(seven), "reason": "슬랙 503", "attempts": 3, "permanent": False}}
    gaps = missing_categories(drafts, "20260930", CATEGORIES, failures)
    assert [(g.number, g.reason) for g in gaps] == [
        (2, "전송 실패(재시도 중단): 이미 발행한 주제"), (4, "원고 없음"), (5, "건너뜀: 이유 없음"),
        (6, "원고 없음"), (7, "전송 실패(재시도 중단): 슬랙 503")]
    # 원고를 고치면(지문이 바뀌면) 예전 실패 기록은 더 이상 막지 않는다
    write_draft(two, good_post(title="🤖 고친 원고"))
    assert 2 not in [g.number for g in missing_categories(drafts, "20260930", CATEGORIES, failures)]
    # 원고 브랜치에 올라간 폴더만 채운 것으로 친다 (로컬에만 있는 원고는 빈 카테고리)
    pushed = [one.name, two.name, three.name]
    local_only = missing_categories(drafts, "20260930", CATEGORIES, failures, pushed)
    assert (7, "원고를 브랜치에 올리지 못함 (로컬에만 있음)") in [(g.number, g.reason) for g in local_only]
    assert missing_categories(tmp_path / "none", "20260930", CATEGORIES)[0].reason == "원고 없음"


def test_helper_refuses_to_judge_from_a_stale_worktree_and_serialises_fillers(branch_repo, monkeypatch, capsys):
    remote, seed, tree = branch_repo
    helper = load_script("cloud_drafts")
    monkeypatch.setattr(helper, "TREE", tree)
    (tree / ".git").exists() or pytest.skip("clone layout")
    git(tree, "remote", "set-url", "origin", str(remote.parent / "gone.git"))  # 원격에 닿지 못함
    assert helper.main(["missing", "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["synced"] is False and "판단하지 않습니다" in out["error"]
    assert helper.main(["setup"]) == 2 and "맞추지 못했습니다" in capsys.readouterr().out
    git(tree, "remote", "set-url", "origin", str(remote))
    assert helper.main(["setup"]) == 0 and helper.SYNC["ok"] is True

    assert helper.claim("autofill") == 0 and helper.lock_holder()["owner"] == "autofill"
    assert helper.claim("manual") == 3  # 다른 쪽이 채우는 중
    assert helper.release("manual") == 3 and helper.lock_holder()  # 남의 잠금은 풀지 않는다
    assert helper.release("autofill") == 0 and helper.lock_holder() == {}
    write_json(helper.lock_path(), {"owner": "old", "at": "2000-01-01T00:00:00+09:00"})
    assert helper.lock_holder() == {} and helper.claim("manual") == 0  # 오래된 잠금은 풀린 것으로 본다
    capsys.readouterr()

    today = helper.today()
    monkeypatch.setattr(helper, "CLOUD_WAIT_UNTIL", (24, 0))
    assert helper.cloud_pending() is True  # 오늘 실행 기록이 아직 없고 기다릴 시각 전
    monkeypatch.setattr(helper, "CLOUD_WAIT_UNTIL", (0, 0))
    assert helper.cloud_pending() is False
    write_json(tree / "runs" / f"{today}.json", {"started": "x"})
    monkeypatch.setattr(helper, "CLOUD_WAIT_UNTIL", (24, 0))
    assert helper.cloud_pending() is False  # 실행 기록이 있으면 기다리지 않는다
    assert helper.main(["missing", "--json"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["synced"] and state["locked_by"] == "manual" and len(state["gaps"]) == 7


def test_published_record_is_kept_when_the_blog_list_cannot_be_read(tmp_path):
    from autopost.history import HistoryUnavailable

    root = tmp_path / "branch"
    write_json(root / "state" / "published.json", [HistoryEntry("2026-09-29", "글", "주제", "AI 도구").to_dict()])

    def unavailable():
        raise HistoryUnavailable("503")

    sync_and_report(root, lambda: datetime(2026, 9, 30, 16, 40), Store(), lambda f: None,
                    SlackNotifier(channel_id="C", client=FakeWebClient()), unavailable)
    assert read_json_list(root / "state" / "published.json")[0]["topic"] == "주제"  # 빈 기록으로 덮지 않음


def read_json_list(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_pending_entries_skip_parked_skipped_later_and_unpushed_drafts(tmp_path):
    from autopost.cloud_drafts import fingerprint, pending_entries, user_topic

    drafts = tmp_path / "drafts"
    first = write_draft(drafts / "20260930_070000_1")
    parked = write_draft(drafts / "20260930_071000_2")
    write_json(drafts / "20260930_072000_3" / "skip.json", {"category": "AI 논문 분석", "reason": "없음"})
    later = write_draft(drafts / "20260930_090000_4")
    failures = {parked.name: {"hash": fingerprint(parked), "permanent": True}}
    now = datetime(2026, 9, 30, 12, 0)
    names = [e.where for e in pending_entries(drafts, now, failures, before=later.name)]
    assert names == [f"최근 원고 {first.name}"]  # 멈춘 원고·건너뜀·뒤에 쓴 원고는 빼고
    entry = pending_entries(drafts, now, failures, include={first.name})[0]
    assert entry.sources == (OFFICIAL, PRESS) and entry.date == "2026-09-30"

    rid = "C0C50SP7VV5-1790734476267849"
    write_json(tmp_path / "runs" / "requests" / f"{rid}.json", {"id": rid, "kind": "single", "topic": "슬랙 에이전트 만들기"})
    assert user_topic(tmp_path, first, rid) and not user_topic(tmp_path, first)
    write_json(tmp_path / "runs" / "requests" / f"{rid}.json", {"id": rid, "kind": "all", "topic": ""})
    assert not user_topic(tmp_path, first, rid)


def test_cloud_check_refuses_stale_or_repeated_news(branch_repo, monkeypatch, capsys):
    remote, seed, tree = branch_repo
    helper = load_script("cloud_drafts")
    monkeypatch.setattr(helper, "TREE", tree)
    monkeypatch.setattr(helper, "setup", lambda: tree)
    monkeypatch.setitem(helper.SYNC, "ok", True)
    monkeypatch.setenv("MIN_BODY_CHARS", "1000")
    today = helper.today()
    stale = write_draft(tree / "drafts" / f"{today}_070000_1", published="2026-01-05")
    assert helper.main(["check", str(stale)]) == 1
    out = capsys.readouterr().out
    assert "최근 3일" in out and "고친 뒤 다시 확인하세요" in out

    first = write_draft(tree / "drafts" / f"{today}_071000_2", post=good_post(category="AI 도구"))
    assert helper.main(["push", str(first)]) == 0
    again = write_draft(tree / "drafts" / f"{today}_072000_4", post=good_post(category="AI 뉴스 리뷰"))
    assert helper.main(["check", str(again)]) == 1  # 방금 올린 원고와 같은 소식 — 카테고리가 달라도
    assert f"최근 원고 {first.name}" in capsys.readouterr().out
    write_json(tree / "state" / "published.json",
               [HistoryEntry("2026-09-01", "옛 글", "NVIDIA Vera CPU 공개", "AI 도구", OFFICIAL).to_dict()])
    assert helper.main(["dupcheck", str(again)]) == 1


def test_review_fixes_for_line_endings_retention_git_and_incomplete_drafts(tmp_path):
    from autopost.cloud_drafts import fingerprint, novelty_problems, pushed_names, sync_drafts

    lf = write_draft(tmp_path / "lf" / "20260930_070000_4")
    crlf = tmp_path / "crlf" / "20260930_070000_4"
    crlf.mkdir(parents=True)
    for name in ("post.json", "research.json"):
        (crlf / name).write_bytes((lf / name).read_bytes().replace(b"\n", b"\r\n"))
    assert fingerprint(lf) == fingerprint(crlf)  # Windows 작업 폴더(CRLF)도 Actions 기록과 같은 지문

    old = {"20260915_070000_1": {"hash": "h", "permanent": True}, "20260801_070000_1": {"hash": "h", "permanent": True}}
    _, records = sync_drafts([], lambda d: None, lambda f: "ts", old, keep_days=["20260915"])
    assert list(records) == ["20260915_070000_1"]  # 겹침 비교 기간 안의 멈춤 기록은 남긴다

    assert pushed_names(tmp_path / "no-tree") == set()  # 작업 폴더가 없어도 죽지 않는다
    half = tmp_path / "tree" / "drafts" / "20260930_070000_4"
    half.mkdir(parents=True)
    (half / "research.json").write_text('{"topic": "t"}', encoding="utf-8")
    assert "읽지 못해" in novelty_problems(half, tmp_path / "tree", datetime(2026, 9, 30, 12))[0]
    wrong = write_draft(tmp_path / "tree" / "drafts" / "20260930_080000_1")  # 폴더 1 = 언어 모델인데 뉴스 리뷰
    assert any("폴더 번호" in p for p in novelty_problems(wrong, tmp_path / "tree", datetime(2026, 9, 30, 12)))


def test_published_record_accumulates_older_posts(tmp_path):
    from autopost import cloud_drafts

    root = tmp_path / "branch"
    old = HistoryEntry("2025-01-01", "아주 옛 글", "옛 주제", "AI 도구", urls={"blogger": "https://b/old"})
    write_json(root / "state" / "published.json", [old.to_dict()])
    fresh = [HistoryEntry("2026-09-29", "글", "주제", "AI 도구", urls={"blogger": "https://b/1"})]
    original = cloud_drafts.merge_published
    try:
        cloud_drafts.merge_published = lambda o, f: original(o, f, limit=1)  # 블로그 목록이 상한에 닿은 경우
        sync_and_report(root, lambda: datetime(2026, 9, 30, 16, 40), Store(), lambda f: None,
                        SlackNotifier(channel_id="C", client=FakeWebClient()), lambda: fresh)
    finally:
        cloud_drafts.merge_published = original
    assert [e["title"] for e in read_json_list(root / "state" / "published.json")] == ["아주 옛 글", "글"]
