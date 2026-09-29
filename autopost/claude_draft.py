"""Claude 가 쓴 원고 → 발행 준비 → 슬랙 승인 요청 (구글 AI API 를 쓰지 않는 흐름).

Claude(데스크톱 앱)가 조사·원고·사실 검증을 끝내고 원고 폴더에 두 파일을 만든다.
    post.json      원고 (writer.OUTPUT_SCHEMA 와 같은 구조 — 제목·도입·한눈에 보기·섹션·FAQ·맺음말·해시태그·source_ids)
    research.json  {"topic", "source_url", "sources": [...], "images": [...], "factcheck": {...}}
여기서 하는 일 (AI 호출 없음):
    출처 주소 확인 → 인용 이미지 내려받기·확인 → 참고문헌(APA) → 대표 이미지 → 이미지 저장소 업로드
    → 발행용 HTML·미리보기 → 원고 보관함(Worker KV)에 맡기기 → 슬랙에 '✅ 누르면 발행' 메시지
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from .blog_publisher import build_labels
from .constants import CATEGORIES, DEFAULT_CATEGORY
from .content import ContentError, PostContent, parse_post
from .cover import make_cover
from .history import load_history
from .links import LinkCheck, check_link, is_http_url
from .publishing import PublishContext, attach_references, base_notes, build_publish_html, write_preview
from .quote_images import parse_candidates
from .research import FactSheet, ImageCandidate, Source
from .slack_helper import LABEL, escape_mrkdwn
from .topic import Topic
from .writer import drop_unknown_images, finalize

log = logging.getLogger(__name__)


class DraftError(RuntimeError):
    """원고 폴더가 잘못됐거나 발행할 수 없는 원고."""


@dataclass(frozen=True)
class DraftFiles:
    content: PostContent
    topic: str
    source_url: str
    sources: tuple[Source, ...]
    candidates: tuple[ImageCandidate, ...]
    factcheck: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class PreparedDraft:
    draft_id: str
    content: PostContent
    html: str
    labels: tuple[str, ...]
    preview: Path | None
    cover: Path
    topic: str
    source_url: str
    fact_line: str
    warnings: tuple[str, ...] = ()

    def bundle(self, now: datetime) -> dict:
        return {"id": self.draft_id, "status": "pending", "title": self.content.title, "html": self.html,
                "labels": list(self.labels), "category": self.content.category, "topic": self.topic,
                "source_url": self.source_url, "created": now.isoformat(timespec="seconds")}


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise DraftError(f"원고 폴더에 {path.name} 가 없습니다: {path.parent}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise DraftError(f"{path.name} 가 JSON 형식이 아닙니다: {exc}") from exc
    if not isinstance(data, dict):
        raise DraftError(f"{path.name} 최상위가 객체가 아닙니다")
    return data


def _sources(raw) -> tuple[Source, ...]:
    result = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and is_http_url(str(item.get("url") or "")):
            result.append(Source(
                id=str(item.get("id") or f"S{len(result) + 1}").strip().upper(),
                title=str(item.get("title") or "").strip() or str(item["url"]),
                url=str(item["url"]).strip(),
                publisher=str(item.get("publisher") or "").strip(),
                published=str(item.get("published") or "").strip()[:10],
                official=bool(item.get("official")),
            ))
    return tuple(result)


def load_draft(folder: Path) -> DraftFiles:
    """원고 폴더(post.json·research.json)를 읽고 형식을 확인한다."""
    research = _read_json(folder / "research.json")
    try:
        category = str(_read_json(folder / "post.json").get("category") or DEFAULT_CATEGORY)
        content = finalize(parse_post((folder / "post.json").read_text(encoding="utf-8-sig"),
                                      category if category in CATEGORIES else DEFAULT_CATEGORY))
    except ContentError as exc:
        raise DraftError(f"post.json 형식 오류: {exc}") from exc
    sources = _sources(research.get("sources"))
    if not sources:
        raise DraftError("research.json 에 출처(sources)가 없습니다")
    topic = str(research.get("topic") or content.title).strip()
    factcheck = research.get("factcheck") if isinstance(research.get("factcheck"), dict) else {}
    return DraftFiles(content, topic, str(research.get("source_url") or "").strip(), sources,
                      parse_candidates(research.get("images")), MappingProxyType(dict(factcheck)))


def fact_line(factcheck: Mapping[str, object]) -> str:
    claims, confirmed = factcheck.get("claims"), factcheck.get("confirmed")
    removed = factcheck.get("removed") or []
    line = (f"주장 {claims}개 중 {confirmed}개 확인 (Claude 검증)" if isinstance(claims, int) and isinstance(confirmed, int)
            else "Claude 검증")
    if isinstance(removed, list) and removed:
        line += f" · 확인 안 된 {len(removed)}곳 뺌"
    return line


class DraftPreparer:
    def __init__(self, blog_name: str, blogger, host, collector, session=None,
                 checker: Callable[..., LinkCheck] = check_link, cover_maker: Callable[..., Path] = make_cover,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self._blog_name = blog_name
        self._blogger = blogger
        self._host = host
        self._collector = collector
        self._session = session
        self._check = checker
        self._cover = cover_maker
        self._now = now

    def _verified(self, sources: Sequence[Source]) -> tuple[tuple[Source, ...], list[str]]:
        kept, warnings = [], []
        for source in sources:
            check = self._check(source.url, (), self._session)
            if check.alive or check.blocked:  # Claude 가 직접 읽은 출처 — 봇 차단(403)은 인정
                kept.append(source)
            else:
                warnings.append(f"출처 제외 (열리지 않음, 상태 {check.status}): {source.url}")
        if not kept:
            raise DraftError("열리는 출처가 하나도 없습니다 — 출처 주소를 확인하세요")
        return tuple(kept), warnings

    def prepare(self, folder: Path, draft_id: str, allow_duplicate: bool = False) -> PreparedDraft:
        files = load_draft(folder)
        now = self._now()
        history = load_history(self._blogger)
        if history.is_duplicate(files.topic, files.source_url) and not allow_duplicate:
            raise DraftError(f"이미 발행한 주제와 겹칩니다: {files.topic} (그래도 올리려면 --allow-duplicate)")
        sources, warnings = self._verified(files.sources)
        images = self._collector.collect(files.candidates, sources, folder) if files.candidates else ()
        if len(images) < len(files.candidates):
            warnings.append(f"인용 이미지 {len(files.candidates) - len(images)}개는 확인을 통과하지 못해 뺐습니다")
        content = drop_unknown_images(files.content, frozenset(i.id for i in images))
        topic = Topic(title=files.topic, category=content.category, angle="", keywords=(files.topic,),
                      source_url=files.source_url)
        sheet = FactSheet(topic, "", (), sources, images=images)
        content = attach_references(content, sheet, now)
        cover = self._cover(folder / "cover.png", content.title, content.category, self._blog_name, f"{now:%Y.%m.%d}")
        ctx = PublishContext(content, cover, base_notes(now, True), history, now.strftime("%Y%m%d_%H%M"), images,
                             files.topic, files.source_url)
        preview = write_preview(ctx, folder, dry_run=True)
        html = build_publish_html(self._host, ctx)
        labels = tuple(build_labels(content.category, content.hashtags))
        (folder / "publish.html").write_text(html, encoding="utf-8")
        return PreparedDraft(draft_id, content, html, labels, preview, cover, files.topic, files.source_url,
                             fact_line(files.factcheck), tuple(warnings))


def build_draft_blocks(prepared: PreparedDraft) -> list[dict]:
    body = (f"*제목:* {escape_mrkdwn(prepared.content.title)}\n*카테고리:* {escape_mrkdwn(prepared.content.category)}\n"
            f"*본문:* {prepared.content.text_length():,}자 · 소제목 {len(prepared.content.sections)}개\n"
            f"*사실 검증:* {escape_mrkdwn(prepared.fact_line)}")
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📝 {LABEL} 발행 대기 원고"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    if prepared.warnings:
        notes = "\n".join(f"• {escape_mrkdwn(w)}" for w in prepared.warnings)[:2000]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*참고*\n{notes}"}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": (
        f"✅ 반응을 누르면 구글 블로그에 발행됩니다 · 미리보기는 스레드의 HTML 파일 · ID `{prepared.draft_id}`")}]})
    return blocks


def send_for_approval(prepared: PreparedDraft, store, slack, channel: str, now: datetime) -> str:
    """원고를 보관함에 맡기고 슬랙에 승인 요청을 올린다 — 슬랙 메시지 위치를 원고에 연결해 둔다."""
    bundle = prepared.bundle(now)
    store.put(prepared.draft_id, bundle)
    ts = slack.post(f"📝 {LABEL} 발행 대기 원고 — {prepared.content.title}", build_draft_blocks(prepared),
                    channel=channel)
    if not ts:
        raise DraftError("슬랙 메시지를 보내지 못했습니다 (봇 토큰·채널 확인)")
    if prepared.preview is not None:
        slack.upload_file(prepared.preview, "구글 블로그 미리보기", "👀 올라갈 모습 — 내려받아 브라우저로 열어 보세요",
                          thread_ts=ts, channel=channel)
    slack.upload_file(prepared.cover, "대표 이미지", "🖼️ 대표 이미지", thread_ts=ts, channel=channel)
    store.put(prepared.draft_id, {**bundle, "slack": {"channel": channel, "ts": ts}})
    return ts
