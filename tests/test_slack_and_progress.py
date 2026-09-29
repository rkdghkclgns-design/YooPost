from autopost.progress import ProgressReporter, build_progress_blocks
from autopost.results import PublishResult
from autopost.slack_helper import (
    SlackNotifier,
    build_report_blocks,
    build_setup_blocks,
    escape_mrkdwn,
    redact,
)
from fakes import FakeWebClient, FakeWebhook


def test_redact_known_secrets_and_patterns():
    text = "key AIza" + "x" * 35 + " xoxb-123-abc ghp_abc https://a?token=zzz Bearer abcdefghij secret123"
    cleaned = redact(text, ["secret123"])
    assert "AIza" not in cleaned and "xoxb" not in cleaned and "ghp_" not in cleaned
    assert "?token=***" in cleaned and "Bearer ***" in cleaned and "secret123" not in cleaned
    assert escape_mrkdwn("<!channel> & `x`") == "&lt;!channel&gt; &amp; 'x'"


def test_report_blocks_for_publish_dry_run_and_held():
    results = [PublishResult("구글 블로그", True, "https://b/1", "발행 완료"), PublishResult.skip("네이버 블로그", "미설정")]
    blocks = build_report_blocks("<제목>", "AI 도구", results, "주장 10개 중 10개 확인")
    assert blocks[0]["text"]["text"].startswith("🤖") and "&lt;제목&gt;" in blocks[1]["text"]["text"]
    assert "<https://b/1|게시글 열기>" in blocks[3]["text"]["text"] and blocks[4]["text"]["text"].startswith("⏭️")
    assert build_report_blocks("t", "c", [], "x", dry_run=True)[0]["text"]["text"].startswith("🧪")
    held = build_report_blocks("t", "c", [], "x", held=True)
    assert "발행 보류" in held[0]["text"]["text"] and "직접 발행" in held[-1]["elements"][0]["text"]
    assert build_report_blocks("t", "c", [PublishResult.fail("네이버 블로그", "x")], "x")[3]["text"]["text"].startswith("❌")


def test_setup_blocks_show_connection_checks():
    blocks = build_setup_blocks([("구글 블로그", True, ""), ("이미지 저장소", False, "IMAGE_<HOST>")], "https://b")
    assert "확인 필요" in blocks[0]["text"]["text"]
    assert "✅ 구글 블로그" in blocks[1]["text"]["text"]
    assert "⬜ 이미지 저장소  _(필요: IMAGE_&lt;HOST&gt;)_" in blocks[1]["text"]["text"]
    footer = blocks[-1]["elements"][0]["text"]
    assert "<https://b|구글 블로그 열기>" in footer and "구글 드라이브는 쓰지 않습니다" in footer
    ok = build_setup_blocks([("슬랙", True, "")])
    assert "연동 점검 완료" in ok[0]["text"]["text"] and "블로그 열기" not in ok[-1]["elements"][0]["text"]


def test_notifier_bot_webhook_fallback_and_uploads(tmp_path):
    client, webhook = FakeWebClient(fail=True), FakeWebhook()
    slack = SlackNotifier(channel_id="C1", client=client, webhook=webhook, secrets=["topsecret"])
    assert slack.post("hello topsecret") is None
    assert webhook.sent[0]["text"] == "hello ***"
    assert not slack.update("1", "x")
    assert not slack.upload_file(tmp_path, "t")
    ok = SlackNotifier(channel_id="C1", client=FakeWebClient())
    assert ok.post("hi") == "1700000000.000001" and ok.update("1700000000.000001", "hi2")
    path = tmp_path / "a.txt"
    path.write_text("x")
    assert ok.upload_file(path, "t", "c")
    only_webhook = SlackNotifier(webhook=FakeWebhook(status_code=500))
    assert only_webhook.enabled and not only_webhook.can_thread and not only_webhook.upload_file(path, "t")
    only_webhook.notify_error("단계", "문제 `x`")
    assert "'단계' 단계에서 오류" in only_webhook._webhook.sent[0]["text"]


def test_progress_updates_one_message_and_routes_stage_outputs(tmp_path):
    client = FakeWebClient()
    reporter = ProgressReporter(SlackNotifier(channel_id="C1", client=client), "🤖 AI 포스팅 진행 중", ["meta"],
                                "https://run", stage_channels={"draft": "CDRAFT"}, run_label="09/29")
    reporter.start()
    reporter.step("topic", "done", "선택")
    reporter.retitle("🤖 AI 포스팅 진행 중 — GPT-6", run_label="GPT-6 · 09/29")
    path = tmp_path / "post.txt"
    path.write_text("x")
    reporter.file(path, "원고", "📝", stage="draft")
    reporter.file(path, "원고2", "📝", stage="draft")
    reporter.note("결과", [{"type": "divider"}], stage="draft")
    reporter.note("스레드 메모")
    reporter.file(None, "없음")
    ts = reporter.thread_ts
    assert client.last_text(ts) == "🤖 AI 포스팅 진행 중 — GPT-6"
    assert client.uploads[0]["channel"] == "CDRAFT" and client.uploads[0]["initial_comment"] == "[GPT-6 · 09/29] 📝"
    pointers = [m for m in client.messages if m.get("thread_ts") == ts and "<#CDRAFT>" in m["text"]]
    assert len(pointers) == 1  # 단계별 채널 안내는 한 번만
    assert client.messages[-2]["blocks"][0]["elements"][0]["text"] == "🤖 GPT-6 · 09/29"
    step = reporter.fail_current(RuntimeError("boom"))
    assert step == "딥리서치 자료 조사"
    assert client.last_text(ts).startswith("❌ AI 포스팅 실패")


def test_progress_without_bot_posts_finished_steps_only():
    webhook = FakeWebhook()
    reporter = ProgressReporter(SlackNotifier(webhook=webhook), "h", [])
    reporter.start()
    reporter.step("topic", "run")
    reporter.step("topic", "done", "ok")
    assert [m["text"] for m in webhook.sent] == ["h", "✅ 주제 선정 — ok"]


def test_progress_blocks_show_icons_and_run_link():
    blocks = build_progress_blocks("h", [], {"topic": ("done", "<x>"), "research": ("run", "")}, "https://run")
    text = blocks[2]["text"]["text"]
    assert "✅ 주제 선정 — &lt;x&gt;" in text and "⏳ *딥리서치 자료 조사* 진행 중…" in text
    assert "<https://run|실행 로그 보기>" in blocks[3]["elements"][0]["text"]
