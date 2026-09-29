"""로컬 .env 파일의 특정 키만 갱신 (주석·순서·다른 값은 유지)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def update_env_file(path: Path, updates: Mapping[str, str]) -> Path:
    for key, value in updates.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key} 값에 줄바꿈이 있어 .env 에 저장할 수 없습니다")

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    result: list[str] = []
    for line in lines:
        match = KEY_RE.match(line)
        key = match.group(1) if match else None
        if key in remaining:
            result.append(f"{key}={remaining.pop(key)}")
        else:
            result.append(line)
    result.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(result) + "\n", encoding="utf-8")
    return path
