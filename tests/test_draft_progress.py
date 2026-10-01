import json
import os
from datetime import datetime, timedelta, timezone

from autopost.draft_progress import ProgressBoard, build_blocks, build_rows, progress_bar, scan
from autopost.slack_helper import SlackNotifier
from fakes import FakeWebClient

NOW = datetime(2026, 9, 29, 14, 0, tzinfo=timezone(timedelta(hours=9)))  # 폴더 이름은 KST


def draft(root, name, title="", sent=False, age_minutes=0):
    folder = root / name
    folder.mkdir(parents=True)
    if title:
        (folder / "post.json").write_text(json.dumps({"title": title}, ensure_ascii=False), encoding="utf-8")
    if sent:
        (folder / "publish.html").write_text("<p/>", encoding="utf-8")
    stamp = NOW.timestamp() - age_minutes * 60
    os.utime(folder, (stamp, stamp))
    return folder


class Store:
    def __init__(self, states):
        self.states = states

    def get(self, draft_id):
        if draft_id not in self.states:
            raise RuntimeError("없음")
        return self.states[draft_id]


def test_rows_show_published_waiting_writing_skipped_and_idle(tmp_path):
    draft(tmp_path, "20260929_130819_1", "Holo4 총정리", sent=True)
    draft(tmp_path, "20260929_131335_1", "GPT-6 가이드", sent=True)
    draft(tmp_path, "20260929_132001_2", "Manus 2.0", sent=True)
    draft(tmp_path, "20260929_132640_3", age_minutes=5)  # 작성 중
    draft(tmp_path, "20260929_090000_4", age_minutes=300)  # 오래된 미완성 → 대기
    draft(tmp_path, "20260928_090000_5", "어제 글", sent=True)  # 다른 날
    (tmp_path / "progress_20260929.json").write_text("{}", encoding="utf-8")
    (tmp_path / "20260929_bad_x").mkdir()
    drafts = scan(tmp_path, "20260929")
    assert [d.category for d in drafts] == ["AI 뉴스 리뷰", "AI 언어 모델", "AI 언어 모델", "AI 도구", "AI 논문 분석"]
    store = Store({"20260929_130819_1": {"status": "published", "url": "https://b/1"},
                   "20260929_132001_2": {"status": "failed"}})
    rows = build_rows(drafts, {"AI 공모주 분석": "최근 공모 없음"}, store, NOW.timestamp())
    icons = [r.icon for r in rows]
    assert icons == ["✅", "❌", "✍️", "⬜", "⬜", "⬜", "⏭️"]
    assert "발행됨: <https://b/1|Holo4 총정리> · 승인 대기: GPT-6 가이드" in rows[0].text
    assert [r.finished for r in rows] == [True, True, False, False, False, False, True]
    approved = build_rows(drafts, {}, Store({"20260929_132001_2": {"status": "approved"}}), NOW.timestamp())
    assert approved[1].icon == "🚀" and approved[0].icon == "📝"  # 보관함에 없으면 승인 대기
    assert build_rows(drafts, {}, None, NOW.timestamp())[0].icon == "📝"
    assert scan(tmp_path / "none", "20260929") == []


def test_blocks_have_progress_bar_and_waiting_hint():
    assert progress_bar(3, 7) == "▰▰▰▱▱▱▱ 3/7"
    from autopost.draft_progress import Row

    text, blocks = build_blocks([Row("AI 도구", "📝", "승인 대기: X", True), Row("AI 보안", "⬜", "대기", False)], "09/29")
    assert "▰▱ 1/2" in text and "오늘 원고 (09/29)" in text
    assert "승인 대기 1편" in blocks[-1]["elements"][0]["text"]
    assert "2. ⬜ *AI 보안* — 대기" in blocks[1]["text"]["text"]
    _, idle = build_blocks([Row("AI 보안", "⬜", "대기", False)], "09/29")
    assert "올라갑니다" in idle[-1]["elements"][0]["text"]


def test_board_posts_once_then_updates_the_same_message(tmp_path):
    draft(tmp_path, "20260929_130819_1", "Holo4", sent=True)
    client = FakeWebClient()
    board = ProgressBoard(SlackNotifier(channel_id="CALERT", client=client), tmp_path,
                          tmp_path / "progress_20260929.json", now=lambda: NOW)
    board.refresh()
    board.skip("AI 공모주 분석", "공모 없음")
    rows = board.refresh()
    assert len(client.messages) == 1 and len(client.updates) == 1  # 같은 메시지를 고친다
    assert rows[-1].icon == "⏭️" and "▰▰▱▱▱▱▱ 2/7" in client.updates[0]["text"]
    state = json.loads((tmp_path / "progress_20260929.json").read_text(encoding="utf-8"))
    assert state["ts"] == "1700000000.000001"
    (tmp_path / "progress_20260929.json").write_text("{broken", encoding="utf-8")
    board.refresh()  # 상태 파일이 깨져도 새 메시지로 이어 간다
    assert len(client.messages) == 2


def test_excluded_drafts_show_as_gaps_not_waiting(tmp_path):
    draft(tmp_path, "20260929_130819_4", "AMD·World Labs 인수", sent=True)
    drafts = scan(tmp_path, "20260929")
    rows = build_rows(drafts, {}, Store({"20260929_130819_4": {"status": "excluded"}}), NOW.timestamp(),
                      failed={"20260929_130819_4": "겹침으로 제외: 발행한 글 'x'"})
    news = rows[3]
    assert news.icon == "🚫" and "겹침으로 제외" in news.text and not news.finished  # 빈칸 — 다른 소식으로 다시
