"""1회용 설정 스크립트 공용: 사용자가 직접 로그인할 브라우저 창 열기 · 값 입력 창."""
from __future__ import annotations


def tk_prompt(title: str, message: str, secret: bool) -> str:  # pragma: no cover - 실제 창을 띄움
    """터미널 없이도 값을 붙여넣을 수 있는 작은 입력 창 (secret 이면 * 로 가림)."""
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    value = simpledialog.askstring(title, message, parent=root, **({"show": "*"} if secret else {}))
    root.destroy()
    return (value or "").strip()


def launch_visible_browser(chromium):
    """설치된 크롬 → 엣지 → Playwright 내장 크롬 순으로 창을 띄운다.

    PC 보안정책에 따라 내장 크롬 창이 막히는 경우(spawn UNKNOWN)가 있어 설치된 브라우저를 먼저 쓴다.
    """
    last_error: Exception | None = None
    for channel in ("chrome", "msedge", None):
        try:
            return chromium.launch(headless=False, **({"channel": channel} if channel else {}))
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"브라우저 창을 열 수 없습니다: {last_error}")
