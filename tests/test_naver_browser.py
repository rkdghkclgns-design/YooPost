"""NaverPublisher 의 Playwright 조작 흐름을 로컬 가짜 에디터로 검증.

실제 네이버 셀렉터 검증이 아니라 '팝업 닫기 → 제목 → 본문/사진 → 태그 → 발행 → URL 추출
→ 세션 저장' 로직이 의도대로 동작하는지 확인한다. Chromium 이 없으면 건너뛴다.
"""
import base64
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from autopost.content import parse_post
from autopost.naver_publisher import NaverExtras, NaverPublisher, NaverSessionExpired, decode_storage_state
from fakes import post_json

FAKE_DIR = Path(__file__).parent / "naver_fake"
EMPTY_STATE = base64.b64encode(json.dumps({"cookies": [], "origins": []}).encode()).decode()


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            playwright.chromium.launch().close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium 미설치")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    handler = functools.partial(_QuietHandler, directory=str(FAKE_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _png(path: Path) -> Path:
    Image.new("RGB", (120, 63), (33, 150, 243)).save(path)
    return path


def _local_storage(encoded_state: str, key: str) -> dict:
    state = decode_storage_state(encoded_state)
    for origin in state["origins"]:
        for item in origin.get("localStorage", []):
            if item["name"] == key:
                return json.loads(item["value"])
    raise AssertionError(f"localStorage '{key}' 없음")


def test_publish_flow(server, tmp_path):
    saved_sessions = []
    publisher = NaverPublisher(
        "myblog", EMPTY_STATE, "publish", tmp_path,
        on_session_updated=lambda value: saved_sessions.append(value) or True,
        write_url=server + "/index.html?blog={blog_id}",
    )
    content = parse_post(post_json())
    result = publisher.publish(content, NaverExtras(_png(tmp_path / "cover.png"), notes="※ 기준일"))

    assert result.ok
    assert result.url == "https://blog.naver.com/myblog/223456789012"
    summary = _local_storage(saved_sessions[0], "summary")
    assert summary["title"] == content.title
    assert summary["images"] == ["cover.png"]
    assert "📰 Gemini 3.8 Live 등장 배경" in summary["body"]
    assert "• 공개일: 2026년 9월 24일" in summary["body"]  # 표는 기호 줄로
    assert "pip install google-genai" in summary["body"]
    assert "❓ 자주 묻는 질문" in summary["body"] and "※ 기준일" in summary["body"]
    assert summary["tags"] == [tag.lstrip("#") for tag in content.hashtags]
    assert summary["align"] == "left"  # 기술 글은 왼쪽 정렬이 기본


def test_center_align_option(server, tmp_path):
    saved = []
    publisher = NaverPublisher("myblog", EMPTY_STATE, "publish", tmp_path, align="center",
                               on_session_updated=lambda v: saved.append(v) or True,
                               write_url=server + "/index.html")
    publisher.publish(parse_post(post_json()))
    assert _local_storage(saved[0], "summary")["align"] == "center"


def test_draft_mode_saves_without_publishing(server, tmp_path):
    saved_sessions = []
    publisher = NaverPublisher(
        "myblog", EMPTY_STATE, "draft", tmp_path,
        on_session_updated=lambda value: saved_sessions.append(value) or True,
        write_url=server + "/index.html",
    )
    content = parse_post(post_json())
    result = publisher.publish(content)
    assert result.ok and result.url == ""
    assert "임시저장" in result.message
    assert _local_storage(saved_sessions[0], "saved")["title"] == content.title


def test_unconfirmed_publish_is_soft_success_to_avoid_duplicates(server, tmp_path):
    publisher = NaverPublisher("myblog", EMPTY_STATE, "publish", tmp_path,
                               write_url=server + "/index_noredirect.html", confirm_timeout_s=1.0)
    result = publisher.publish(parse_post(post_json()))
    assert result.ok and result.url == "https://blog.naver.com/myblog"
    assert "확인하지 못했습니다" in result.message
    assert list(tmp_path.glob("naver_unconfirmed_*.png"))


def test_expired_session_raises_and_saves_screenshot(server, tmp_path):
    publisher = NaverPublisher("myblog", EMPTY_STATE, "publish", tmp_path, write_url=server + "/nidlogin.html")
    with pytest.raises(NaverSessionExpired):
        publisher.publish(parse_post(post_json()))
    assert list(tmp_path.glob("naver_error_*.png"))
