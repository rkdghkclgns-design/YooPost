"""발행 기록 — 같은 주제 반복 방지 · '함께 보면 좋은 글' 링크.

드라이브 루트의 posted_history.json 한 파일에 저장한다 (사람이 열어봐도 읽을 수 있는 JSON).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .constants import HISTORY_FILE
from .links import canonical

log = logging.getLogger(__name__)

SIMILAR_TITLE = 0.72  # 제목이 이만큼 비슷하면 같은 주제로 본다
MAX_ENTRIES = 1000
_NOISE = re.compile(r"[^0-9a-z가-힣]+")


def _norm(text: str) -> str:
    return _NOISE.sub("", text.lower())


@dataclass(frozen=True)
class HistoryEntry:
    date: str  # YYYY-MM-DD
    title: str  # 발행한 글 제목
    topic: str  # 주제 (원 기사·발표 제목)
    category: str
    source_url: str = ""
    urls: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))  # 플랫폼 → 게시글 주소

    def to_dict(self) -> dict:
        return {"date": self.date, "title": self.title, "topic": self.topic, "category": self.category,
                "source_url": self.source_url, "urls": dict(self.urls)}

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        return cls(
            date=str(data.get("date", "")),
            title=str(data.get("title", "")),
            topic=str(data.get("topic", "")),
            category=str(data.get("category", "")),
            source_url=str(data.get("source_url", "")),
            urls=MappingProxyType({str(k): str(v) for k, v in (data.get("urls") or {}).items()}),
        )


@dataclass(frozen=True)
class PostHistory:
    entries: tuple[HistoryEntry, ...] = ()

    def is_duplicate(self, topic: str, url: str = "") -> bool:
        """같은 원문 주소이거나, 주제·제목이 거의 같은 글이 이미 있으면 True."""
        key = canonical(url) if url else ""
        wanted = _norm(topic)
        for entry in self.entries:
            if key and entry.source_url and canonical(entry.source_url) == key:
                return True
            for seen in (entry.topic, entry.title):
                norm = _norm(seen)
                if wanted and norm and SequenceMatcher(None, wanted, norm).ratio() >= SIMILAR_TITLE:
                    return True
        return False

    def recent_titles(self, limit: int = 40) -> list[str]:
        return [e.title for e in self.entries[-limit:]][::-1]

    def related(self, platform: str, category: str, limit: int) -> list[tuple[str, str]]:
        """같은 카테고리 최근 글 우선, 모자라면 다른 최근 글 — (제목, 주소)."""
        published = [e for e in reversed(self.entries) if e.urls.get(platform)]
        same = [e for e in published if e.category == category]
        others = [e for e in published if e.category != category]
        return [(e.title, e.urls[platform]) for e in (same + others)[:limit]]

    def add(self, entry: HistoryEntry) -> "PostHistory":
        return replace(self, entries=(*self.entries, entry)[-MAX_ENTRIES:])

    def to_json(self) -> str:
        return json.dumps({"posts": [e.to_dict() for e in self.entries]}, ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "PostHistory":
        try:
            data = json.loads(text or "{}")
        except json.JSONDecodeError:
            log.warning("발행 기록 파일이 손상되어 빈 기록으로 시작합니다")
            return cls()
        posts = data.get("posts", []) if isinstance(data, dict) else []
        return cls(tuple(HistoryEntry.from_dict(p) for p in posts if isinstance(p, dict)))


class HistoryStore:
    """드라이브 루트 폴더의 posted_history.json 읽기/쓰기."""

    def __init__(self, drive, root_id: str, workdir: Path) -> None:
        self._drive = drive
        self._root_id = root_id
        self._workdir = workdir

    def load(self) -> PostHistory:
        found = self._drive.find_child(self._root_id, HISTORY_FILE)
        if found is None:
            return PostHistory()
        path = self._drive.download(found.id, self._workdir / HISTORY_FILE)
        return PostHistory.from_json(path.read_text(encoding="utf-8"))

    def save(self, history: PostHistory) -> None:
        path = self._workdir / HISTORY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(history.to_json(), encoding="utf-8")
        found = self._drive.find_child(self._root_id, HISTORY_FILE)
        if found is None:
            self._drive.upload(self._root_id, path, name=HISTORY_FILE, mime_type="application/json")
        else:
            self._drive.update_content(found.id, path, mime_type="application/json")
