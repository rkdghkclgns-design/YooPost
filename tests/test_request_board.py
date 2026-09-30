"""#yoopost-알림 수동 지시 진행 카드 — 단계 막대·카테고리 줄·접수 때 올리고 동기화 때 고치기."""
import json
from datetime import datetime, timedelta, timezone

from autopost.cloud_drafts import sync_and_report, write_json
from autopost.draft_request import new_record, request_path, with_request
from autopost.manual_request import FireResult, handle_request
from autopost.request_board import board_hash, board_rows, build_board, valid_ts
from autopost.slack_helper import SlackNotifier
from fakes import FakeWebClient
from test_cloud_drafts import SENT, Store, good_post, write_draft

KST = timezone(timedelta(hours=9))
MANUAL, ALERT = "C0MANUAL01", "C0ALERT001"
NOW = datetime(2026, 9, 30, 16, 40, tzinfo=KST)
TS = f"{int(NOW.timestamp()) - 600}.000100"
RID = f"{MANUAL}-{TS.replace('.', '')}"


def record(**extra):
    return {**new_record(RID, "single", "AI 도구", "슬랙에 에이전트 직원 만들기", "U0C4PLGBNF9",
                         (NOW - timedelta(minutes=10)).isoformat()), **extra}


def head(request, folders=(), lookup=None, failures=None):
    text, blocks = build_board(request, board_rows(request, list(folders), lookup, failures or {}), "https://link")
    return text, json.dumps(blocks, ensure_ascii=False)


def test_single_request_walks_through_the_four_stages(tmp_path):
    text, body = head(record())
    assert "1편 · AI 도구" in text and "▰▱▱▱ 1/4 접수" in text
    assert "«슬랙에 에이전트 직원 만들기»" in body and "<@U0C4PLGBNF9>" in body and "지시 스레드" in body
    started = record(started=NOW.isoformat())
    assert "▰▰▱▱ 2/4 작성·검증 중" in head(started)[0]
    folder = write_draft(tmp_path / "20260930_163000_2", post=good_post(category="AI 도구"))
    assert "원고 전송 중" in head(started, [folder])[0]
    assert "▰▰▰▱ 3/4 승인 대기" in head(started, [folder], lambda _: SENT)[0]
    published = {**SENT, "status": "published", "url": "https://blog/p/1"}
    text, body = head(started, [folder], lambda _: published)
    assert "▰▰▰▰ 4/4 발행됨" in text and "https://blog/p/1" in body


def test_endings_skip_missing_and_fire_failure(tmp_path):
    skip = tmp_path / "20260930_163500_2"
    write_json(skip / "skip.json", with_request({"category": "AI 도구", "reason": "공식 문서 없음"}, RID, "single"))
    assert "4/4 건너뜀" in head(record(started="x"), [skip])[0]
    assert "4/4 원고 없음" in head(record(started="x", finished="y"))[0]
    assert "4/4 원고 작성을 시작하지 못함" in head(record(fire_error="HTTP 429"))[0]
    broken = write_draft(tmp_path / "20260930_163000_2", post=good_post(category="AI 도구"))
    text, body = head(record(started="x"), [broken], lambda _: None, {broken.name: {"reason": "중복"}})
    assert "승인 요청 전송 실패" in text


def test_all_request_counts_categories():
    request = {**record(), "kind": "all", "category": "", "topic": "", "started": NOW.isoformat()}
    text, body = head(request)
    assert "7개 카테고리 새 글" in text and "0/7" in text and "지시: 전체 새 글" in body
    assert body.count("작성·검증 중") == 1 and body.count("대기") >= 6


def test_helpers():
    assert valid_ts("1790734476.267849") == "1790734476.267849" and valid_ts("x") == "" and valid_ts(None) == ""
    assert board_hash("a", []) == board_hash("a", []) != board_hash("b", [])


def test_the_card_is_posted_on_intake_and_closed_when_firing_fails():
    client = FakeWebClient()
    notifier = SlackNotifier(client=client, channel_id=MANUAL)
    saved = []
    status = handle_request("슬랙에 에이전트 직원 만들기를 AI도구에 발행해줘", MANUAL, TS, "U0C4PLGBNF9", "message",
                            manual_channel=MANUAL, notifier=notifier, fire=lambda t: FireResult(False, error="HTTP 429"),
                            record=saved.append, now=NOW, alert_channel=ALERT)
    assert status == "failed"
    card = next(m for m in client.messages if m["channel"] == ALERT)
    assert "수동 지시" in card["text"] and "1/4 접수" in card["text"]
    assert saved[0]["alert_ts"] and saved[-1]["fire_error"] == "HTTP 429"  # 카드 위치를 요청 기록에 남긴다
    assert client.updates[-1]["channel"] == ALERT and "시작하지 못함" in client.updates[-1]["text"]


def test_sync_updates_the_card_only_when_it_changes(tmp_path):
    root = tmp_path / "branch"
    client = FakeWebClient()
    slack = SlackNotifier(channel_id=ALERT, client=client)
    store = Store()
    write_json(request_path(root, RID), record(started=NOW.isoformat(), alert_ts="1790000000.000009"))

    def sync():
        return sync_and_report(root, lambda: NOW, store, lambda f: (store.items.__setitem__(f.name, SENT), "1790000000.000001")[1],
                               slack, lambda: [], manual_channel=MANUAL, draft_channel="CDRAFT")

    sync()
    assert client.updates[-1]["ts"] == "1790000000.000009" and "작성·검증 중" in client.updates[-1]["text"]
    sync()
    assert len(client.updates) == 1  # 그대로면 고치지 않는다
    folder = write_draft(root / "drafts" / "20260930_163000_2", post=good_post(category="AI 도구"))
    write_json(folder / "research.json", with_request(json.loads((folder / "research.json").read_text(encoding="utf-8")),
                                                    RID, "single"))
    sync()
    assert "3/4 승인 대기" in client.updates[-1]["text"]

    # 카드 위치가 없는 요청(이 기능 전에 접수)은 새 카드를 올리고 그 위치를 기억한다
    old_rid = f"{MANUAL}-{int(NOW.timestamp()) - 900}000100"
    write_json(request_path(root, old_rid), {**record(started=NOW.isoformat()), "id": old_rid})
    posted_before = len([m for m in client.messages if m["channel"] == ALERT])
    sync()
    sync()
    assert len([m for m in client.messages if m["channel"] == ALERT and "수동 지시" in m["text"]]) == posted_before + 1
