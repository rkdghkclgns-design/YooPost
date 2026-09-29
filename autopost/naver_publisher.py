"""네이버 블로그 발행 (Playwright + 저장된 로그인 세션).

네이버는 블로그 글쓰기 공개 API가 없어 스마트에디터 ONE 화면을 브라우저로 조작한다.
- 로그인은 scripts/export_naver_cookies.py 로 사람이 직접 1회 수행한 세션을 재사용한다.
- 네이버 UI가 바뀌면 NaverSelectors 값만 수정하면 된다.
- 에디터에는 HTML 을 넣을 수 없어 박스·표는 굵은 글씨·기호 줄로 바꿔 입력한다 (구성 순서는 구글 블로그와 같음).
- 실패 시 스크린샷을 OUTPUT_DIR 에 남기고, 파이프라인이 슬랙으로 수동 발행용 원고를 보낸다.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlparse

from .constants import CLOSING_HEADING, FAQ_HEADING, REFERENCES_HEADING, RELATED_HEADING, SUMMARY_TITLE
from .content import Block, PostContent, bold_segments
from .results import PublishResult

log = logging.getLogger(__name__)

PLATFORM = "네이버 블로그"
WRITE_URL = "https://blog.naver.com/{blog_id}/postwrite"
POST_ID_RE = re.compile(r"(?:logNo=|/)(\d{9,})")
MAX_TAGS = 30
HEADING_RULE = "━━━━━━━━━━━━━━━━"  # h2 소제목 위·아래 줄 (구글 블로그 소제목 밑줄과 비슷한 강조)


class NaverError(RuntimeError):
    pass


class NaverSessionExpired(NaverError):
    pass


@dataclass(frozen=True)
class NaverSelectors:
    main_frame: str = "iframe#mainFrame"
    popup_cancel: str = "button.se-popup-button-cancel"
    help_close: str = "button.se-help-panel-close-button"
    title: str = ".se-documentTitle .se-text-paragraph"
    body_paragraph: str = ".se-component.se-text .se-text-paragraph"
    image_button: str = "button.se-image-toolbar-button"
    image_component: str = ".se-component.se-image"
    publish_button: str = "button[class*='publish_btn']"
    tag_input: str = "input[class*='tag_input']"
    confirm_publish: str = "button[class*='confirm_btn']"
    save_button: str = "button[class*='save_btn']"
    align_dropdown: str = "button.se-property-toolbar-drop-down-button[data-name^='align']"
    align_center: str = "button.se-toolbar-option-align-center-button"


@dataclass(frozen=True)
class NaverBlock:
    kind: str  # "text"(**굵게** 지원) | "code"(그대로) | "heading"(h2) | "subheading"(h3) | "image"
    text: str = ""
    image: Path | None = None


@dataclass(frozen=True)
class NaverExtras:
    cover: Path | None = None
    related: tuple[tuple[str, str], ...] = ()
    notes: str = ""


def _table_lines(block: Block) -> list[str]:
    if len(block.headers) == 2:
        return [f"• **{row[0]}**: {row[1]}" for row in block.rows]
    lines = []
    for row in block.rows:
        rest = " / ".join(f"{h}: {c}" for h, c in zip(block.headers[1:], row[1:]) if c)
        lines.append(f"• **{row[0]}** — {rest}" if rest else f"• **{row[0]}**")
    return lines


def _box_text(icon: str, title: str, text: str) -> str:
    head = title.strip()
    if head and not head.startswith(icon):
        head = f"{icon} {head}"
    return f"**{head}** — {text}" if head else f"{icon} {text}"


def convert_block(block: Block) -> NaverBlock:
    if block.kind == "subheading":
        return NaverBlock("subheading", block.text)
    if block.kind == "info":
        return NaverBlock("text", _box_text("💡", block.title, block.text))
    if block.kind == "warning":
        return NaverBlock("text", _box_text("⚠️", block.title, block.text))
    if block.kind == "table":
        return NaverBlock("text", "\n".join(_table_lines(block)))
    if block.kind == "code":
        return NaverBlock("code", block.text)
    if block.kind == "list":
        return NaverBlock("text", "\n".join(f"• {item}" for item in block.items))
    return NaverBlock("text", block.text.replace("\n\n", "\n"))


def build_blocks(content: PostContent, extras: NaverExtras = NaverExtras()) -> tuple[NaverBlock, ...]:
    """도입 → 대표 이미지 → 한눈에 보기 → 섹션들 → FAQ → 함께 보면 좋은 글 → 맺음말 → 참고 문헌 → 기준일."""
    blocks = [NaverBlock("text", content.intro)]
    if extras.cover is not None:
        blocks.append(NaverBlock("image", image=extras.cover))
    if content.summary:
        blocks.append(NaverBlock("text", f"**📌 {SUMMARY_TITLE}**\n" + "\n".join(f"- {s}" for s in content.summary)))
    for section in content.sections:
        blocks.append(NaverBlock("heading", section.heading))
        blocks.extend(convert_block(block) for block in section.blocks)
    if content.faq:
        blocks.append(NaverBlock("heading", FAQ_HEADING))
        blocks.extend(NaverBlock("text", f"**Q{i}. {item.question}**\n{item.answer}")
                      for i, item in enumerate(content.faq, start=1))
    if extras.related:
        blocks.append(NaverBlock("heading", RELATED_HEADING))
        blocks.append(NaverBlock("text", "\n".join(f"• {title}\n{url}" for title, url in extras.related)))
    blocks += [NaverBlock("heading", CLOSING_HEADING), NaverBlock("text", content.closing)]
    if content.references:
        blocks.append(NaverBlock("heading", REFERENCES_HEADING))
        blocks.append(NaverBlock("code", "\n".join(f"• {r.apa()}" for r in content.references)))
    if extras.notes:
        blocks.append(NaverBlock("text", extras.notes))
    return tuple(blocks)


def extract_post_url(blog_id: str, urls: Sequence[str]) -> str:
    for url in urls:
        if blog_id not in url:
            continue
        match = POST_ID_RE.search(url)
        if match:
            return f"https://blog.naver.com/{blog_id}/{match.group(1)}"
    return ""


NAVER_DOMAIN = "naver.com"
MAX_SECRET_CHARS = 45_000  # GitHub 시크릿 한도(48KB) 여유


def _host_allowed(host: str, extra_hosts: Sequence[str]) -> bool:
    host = host.lstrip(".").lower()
    return host == NAVER_DOMAIN or host.endswith("." + NAVER_DOMAIN) or host in extra_hosts


def encode_storage_state(state: dict, extra_hosts: Sequence[str] = ()) -> str:
    """로그인 세션을 시크릿에 넣을 base64 문자열로 — 네이버(+글쓰기 주소) 쿠키·저장소만 남겨 크기를 줄인다."""
    cookies = [c for c in state.get("cookies", []) if _host_allowed(str(c.get("domain", "")), extra_hosts)]
    origins = [
        o for o in state.get("origins", [])
        if _host_allowed(urlparse(str(o.get("origin", ""))).hostname or "", extra_hosts)
    ]

    def encode(compact: dict) -> str:
        return base64.b64encode(json.dumps(compact, ensure_ascii=False).encode("utf-8")).decode("ascii")

    encoded = encode({"cookies": cookies, "origins": origins})
    if len(encoded) > MAX_SECRET_CHARS:  # 로그인은 쿠키만으로 유지되므로 저장소 데이터는 버린다
        encoded = encode({"cookies": cookies, "origins": []})
    return encoded


def decode_storage_state(encoded: str) -> dict:
    try:
        return json.loads(base64.b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise NaverError("NAVER_STORAGE_STATE_B64 값을 해석할 수 없습니다 (base64 JSON 이어야 함)") from exc


class NaverPublisher:
    def __init__(
        self,
        blog_id: str,
        storage_state_b64: str,
        mode: str,
        screenshot_dir: Path,
        selectors: NaverSelectors = NaverSelectors(),
        headless: bool = True,
        on_session_updated: Callable[[str], bool] | None = None,
        write_url: str = WRITE_URL,
        confirm_timeout_s: float = 30.0,
        align: str = "left",
    ) -> None:
        self._confirm_polls = max(1, int(confirm_timeout_s * 2))
        self._blog_id = blog_id
        self._storage_state_b64 = storage_state_b64
        self._mode = mode
        self._screenshot_dir = screenshot_dir
        self._sel = selectors
        self._headless = headless
        self._on_session_updated = on_session_updated
        self._write_url = write_url
        self._align = align

    def publish(self, content: PostContent, extras: NaverExtras = NaverExtras()) -> PublishResult:
        from playwright.sync_api import sync_playwright

        state = decode_storage_state(self._storage_state_b64)
        blocks = build_blocks(content, extras)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self._headless)
            context = browser.new_context(
                storage_state=state, locale="ko-KR", viewport={"width": 1400, "height": 1000}
            )
            page = context.new_page()
            try:
                url, message = self._write(page, content, blocks)
                self._save_session(context)
                return PublishResult(platform=PLATFORM, ok=True, url=url, message=message)
            except Exception:
                self._screenshot(page, "naver_error")
                raise
            finally:
                context.close()
                browser.close()

    # ----- 내부 단계 -----
    def _write(self, page, content: PostContent, blocks: Sequence[NaverBlock]) -> tuple[str, str]:
        page.goto(self._write_url.format(blog_id=self._blog_id), wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4000)
        if "nid.naver.com" in page.url or "nidlogin" in page.url:
            raise NaverSessionExpired(
                "네이버 로그인 세션이 만료되었습니다. scripts/export_naver_cookies.py 로 다시 저장하세요"
            )
        editor = page.frame_locator(self._sel.main_frame) if page.locator(self._sel.main_frame).count() else page
        self._dismiss_popups(editor, page)

        editor.locator(self._sel.title).first.click()
        page.keyboard.insert_text(content.title)
        editor.locator(self._sel.body_paragraph).first.click()
        if self._align == "center":
            self._align_center(page, editor)
        for block in blocks:
            if block.kind == "image" and block.image is not None:
                self._insert_image(page, editor, block.image)
            elif block.kind == "heading":
                self._type_heading(page, block.text)
            elif block.kind == "subheading":
                self._type_rich(page, f"**▶ {block.text.strip()}**")
            elif block.kind == "code":
                self._type_plain(page, block.text)
            else:
                self._type_rich(page, block.text)
        return self._finish(page, editor, content.hashtags)

    def _dismiss_popups(self, editor, page) -> None:
        for selector in (self._sel.popup_cancel, self._sel.help_close):
            button = editor.locator(selector)
            if button.count():
                try:
                    button.first.click(timeout=3000)
                    page.wait_for_timeout(500)
                except Exception as exc:
                    log.debug("팝업 닫기 무시: %s", exc)

    def _align_center(self, page, editor) -> None:
        """본문 가운데 정렬 — '정렬' 드롭다운을 열고 '가운데 정렬'을 누른다 (이후 입력하는 문단에 이어짐)."""
        try:
            editor.locator(self._sel.align_dropdown).first.click(timeout=3000)
            editor.locator(self._sel.align_center).first.click(timeout=3000)
        except Exception as exc:  # 정렬 실패는 발행을 막지 않는다
            log.warning("가운데 정렬 설정 실패 (왼쪽 정렬로 진행): %s", exc)

    @staticmethod
    def _type_line(page, line: str) -> None:
        """한 줄 입력 — **굵게** 부분은 Ctrl+B 로 켜고 끈다."""
        for text, bold in bold_segments(line):
            if not text:
                continue
            if bold:
                page.keyboard.press("Control+b")
            page.keyboard.insert_text(text)
            if bold:
                page.keyboard.press("Control+b")

    def _type_rich(self, page, text: str) -> None:
        for line in text.split("\n"):
            if line.strip():
                self._type_line(page, line.strip())
            page.keyboard.press("Enter")
        page.keyboard.press("Enter")  # 블록 사이 빈 줄

    @staticmethod
    def _type_plain(page, text: str) -> None:
        for line in text.split("\n"):
            if line.strip():
                page.keyboard.insert_text(line.rstrip())
            page.keyboard.press("Enter")
        page.keyboard.press("Enter")

    def _type_heading(self, page, text: str) -> None:
        """h2 강조: 위아래 줄 + 굵은 글씨 (에디터 기능 대신 입력만으로 — 화면 구조 변경에 강함)."""
        page.keyboard.insert_text(HEADING_RULE)
        page.keyboard.press("Enter")
        self._type_line(page, f"**{text.strip()}**")
        page.keyboard.press("Enter")
        page.keyboard.insert_text(HEADING_RULE)
        page.keyboard.press("Enter")
        page.keyboard.press("Enter")

    def _insert_image(self, page, editor, path: Path) -> None:
        before = editor.locator(self._sel.image_component).count()
        with page.expect_file_chooser(timeout=15_000) as chooser:
            editor.locator(self._sel.image_button).first.click()
        chooser.value.set_files(str(path))
        for _ in range(60):
            if editor.locator(self._sel.image_component).count() > before:
                break
            page.wait_for_timeout(500)
        else:
            raise NaverError(f"이미지 업로드가 완료되지 않았습니다: {path.name}")
        page.wait_for_timeout(800)
        editor.locator(self._sel.body_paragraph).last.click()
        page.keyboard.press("End")

    def _finish(self, page, editor, hashtags: Sequence[str]) -> tuple[str, str]:
        if self._mode == "draft":
            editor.locator(self._sel.save_button).first.click()
            page.wait_for_timeout(3000)
            return "", "임시저장 완료 — 네이버 블로그 '임시저장 글'에서 확인 후 발행하세요"

        editor.locator(self._sel.publish_button).first.click()
        page.wait_for_timeout(1500)
        tag_input = editor.locator(self._sel.tag_input).first
        for tag in list(hashtags)[:MAX_TAGS]:
            tag_input.click()
            page.keyboard.insert_text(tag.lstrip("#"))
            page.keyboard.press("Enter")
        editor.locator(self._sel.confirm_publish).first.click()

        for _ in range(self._confirm_polls):
            url = extract_post_url(self._blog_id, [page.url, *(frame.url for frame in page.frames)])
            if url:
                return url, "발행 완료"
            page.wait_for_timeout(500)
        # 발행 버튼까지 눌렀다면 실제로는 게시됐을 가능성이 높다. 실패로 처리하면 수동 원고 안내로
        # 중복 게시가 생길 수 있어, 경고와 함께 성공으로 보고하고 사람이 확인하도록 한다.
        self._screenshot(page, "naver_unconfirmed")
        return (
            f"https://blog.naver.com/{self._blog_id}",
            "⚠️ 발행 버튼은 눌렀지만 게시글 주소를 확인하지 못했습니다. 블로그에서 게시 여부를 확인하세요 "
            "(원고는 슬랙 원고 채널의 post.txt)",
        )

    def _save_session(self, context) -> None:
        if self._on_session_updated is None:
            return
        try:
            write_host = urlparse(self._write_url).hostname or ""
            self._on_session_updated(encode_storage_state(context.storage_state(), extra_hosts=(write_host,)))
        except Exception as exc:
            log.warning("네이버 세션 저장 실패 (다음 실행에는 영향 없음): %s", exc)

    def _screenshot(self, page, name: str) -> None:
        try:
            self._screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self._screenshot_dir / f"{name}_{int(time.time())}.png"
            page.screenshot(path=str(path), full_page=True)
            log.info("네이버 오류 스크린샷 저장: %s", path)
        except Exception as exc:
            log.warning("스크린샷 저장 실패: %s", exc)
