"""1회용 설정 스크립트 공용: 값 입력 창."""
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
