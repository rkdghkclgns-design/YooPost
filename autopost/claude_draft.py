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
from .freshness import folder_date, freshness_problems, page_date_check, page_published, primary_source
from .history import HistoryEntry, HistoryUnavailable, PostHistory, combined, load_history
from .links import LinkCheck, check_link, is_http_url
from .publishing import PublishContext, attach_references, base_notes, build_publish_html, write_preview
from .quote_images import AUTO_IMAGES, auto_candidates, fetch_page, keywords_of, parse_candidates
from .research import FactSheet, ImageCandidate, Source
from .slack_helper import LABEL, escape_mrkdwn
from .topic import Topic
from .writer import drop_unknown_images, finalize, place_images

log = logging.getLogger(__name__)


class DraftError(RuntimeError):
    """원고 폴더가 잘못됐거나 발행할 수 없는 원고."""


def folder_category(name: str) -> str:
    """원고 폴더 이름 끝 번호(YYYYMMDD_HHMMSS_n)의 카테고리 — 번호가 없거나 0 이면 빈 문자열."""
    suffix = name.rsplit("_", 1)[-1] if "_" in name else ""
    return CATEGORIES[int(suffix) - 1] if suffix.isdigit() and 1 <= int(suffix) <= len(CATEGORIES) else ""


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
    sources_md: Path | None = None  # 출처·사실 검증 목록 (슬랙 스레드에 올려 ✅ 전에 확인)
    sources: tuple[str, ...] = ()  # 출처 주소 — 발행 직전·승인 대기 중 겹침 확인에 쓴다

    def bundle(self, now: datetime) -> dict:
        return {"id": self.draft_id, "status": "pending", "title": self.content.title, "html": self.html,
                "labels": list(self.labels), "category": self.content.category, "topic": self.topic,
                "source_url": self.source_url, "sources": list(self.sources), "created": now.isoformat(timespec="seconds")}


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


def sources_markdown(files: DraftFiles, sources: Sequence[Source], line: str) -> str:
    """승인 전에 볼 출처·사실 검증 목록."""
    lines = [f"# 출처·사실 검증 — {files.content.title}", "", f"원문: {files.source_url or '없음'}", f"사실 검증: {line}",
             "", "## 출처"]
    lines += [f"- [{s.id}] {'(공식) ' if s.official else ''}{s.publisher} — {s.title} ({s.published or '날짜 미상'})\n  {s.url}"
              for s in sources]
    removed = files.factcheck.get("removed") or []
    if isinstance(removed, list) and removed:
        lines += ["", "## 확인되지 않아 뺀 문장"] + [f"- {r}" for r in removed]
    note = files.factcheck.get("note")
    if note:
        lines += ["", f"검증 방법: {note}"]
    return "\n".join(lines) + "\n"


def fact_line(factcheck: Mapping[str, object]) -> str:
    claims, confirmed = factcheck.get("claims"), factcheck.get("confirmed")
    removed = factcheck.get("removed") or []
    line = (f"주장 {claims}개 중 {confirmed}개 확인 (Claude 검증)" if isinstance(claims, int) and isinstance(confirmed, int)
            else "Claude 검증")
    if isinstance(removed, list) and removed:
        line += f" · 확인 안 된 {len(removed)}곳 뺌"
    return line


def draft_keywords(files: DraftFiles) -> set[str]:
    """본문 그림 관련도 기준 낱말 — 제목·주제·소제목·해시태그·한눈에 보기 (본문 전체는 넣지 않는다: 흔한 말로 아무 그림이나 붙지 않게)."""
    content = files.content
    return keywords_of(content.title, files.topic, *(s.heading for s in content.sections), *content.hashtags,
                       *content.summary)


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
        self._history_cache: PostHistory | None = None

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

    def _history(self, strict: bool) -> PostHistory:
        """발행 기록 — 이 준비기(Actions 한 번 실행)에서 한 번만 읽는다 (원고마다 글 목록 전체를 받지 않도록).

        strict 이면 못 읽을 때 HistoryUnavailable (중복 검사를 빈 기록으로 하면 안 된다 — 다음 실행에 다시).
        """
        if self._history_cache is None:
            try:
                self._history_cache = load_history(self._blogger, strict=True)
            except HistoryUnavailable:
                if strict:
                    raise
                return PostHistory()
        return self._history_cache

    def _check_overlap(self, files: DraftFiles, pending: Sequence[HistoryEntry]) -> None:
        """발행한 글 전부 + 아직 발행 전인 원고와 같은 소식이면 원고 문제."""
        found = combined(self._history(strict=True), pending).find_duplicate(
            files.topic, files.source_url, files.content.title, [s.url for s in files.sources])
        if found:
            entry, reason = found
            raise DraftError(f"이미 쓴 글과 겹칩니다 — {entry.where or '발행한 글'} '{entry.title}' ({reason}) "
                             "— 다른 소식으로 다시 쓰세요")

    def _check_fresh(self, files: DraftFiles, draft_id: str, now: datetime) -> list[str]:
        """최신 이슈인지 (원고를 쓴 날 기준, Claude 가 직접 읽은 출처 목록으로) — 아니면 원고 문제.

        러너에서 출처가 잠깐 안 열려도 판단이 바뀌지 않게 링크 확인 전 목록을 쓴다. 원문 페이지 발표일이 기간 밖이면 거절,
        원고 표기와 다르면 참고 문구 (페이지를 못 열거나 날짜 표시가 없으면 원고 표기를 따른다).
        """
        category, day = files.content.category, folder_date(draft_id, now.date())
        problems = freshness_problems(category, files.sources, files.source_url, day)
        if problems:
            raise DraftError("최신 이슈가 아닙니다 — " + " / ".join(problems))
        primary = primary_source(files.sources, files.source_url)
        page = fetch_page(self._session, primary.url) if primary else ""
        reject, note = page_date_check(primary.published if primary else "", page_published(page), category, day)
        if reject:
            raise DraftError(f"최신 이슈가 아닙니다 — {reject}")
        return [note] if note else []

    def prepare(self, folder: Path, draft_id: str, allow_duplicate: bool = False,
                pending: Sequence[HistoryEntry] = (), fresh_required: bool = True) -> PreparedDraft:
        """pending = 아직 발행 전인 다른 원고 (중복 비교), fresh_required = 최신 이슈 조건 확인 (사용자가 주제를 정한 지시는 끔)."""
        files = load_draft(folder)
        now = self._now()
        numbered = folder_category(draft_id)
        if numbered and numbered != files.content.category:
            raise DraftError(f"post.json category '{files.content.category}' 가 원고 폴더 번호의 카테고리 '{numbered}' 와 다릅니다")
        if not allow_duplicate:
            self._check_overlap(files, pending)
        sources, warnings = self._verified(files.sources)
        if fresh_required:
            warnings += self._check_fresh(files, draft_id, now)
        keywords = draft_keywords(files)
        images = self._collector.collect(files.candidates, sources, folder, keywords) if files.candidates else ()
        if len(images) < len(files.candidates):
            warnings.append(f"인용 이미지 {len(files.candidates) - len(images)}개는 확인을 통과하지 못해 뺐습니다 "
                            "(썸네일·로고이거나, 원고와 관련된 본문 그림을 찾지 못함)")
        content = drop_unknown_images(files.content, frozenset(i.id for i in images))
        if not images:  # 원고에 쓸 이미지가 없으면 공식 출처 본문에서 원고와 관련된 그림만 보충 (썸네일은 쓰지 않음)
            images = self._collector.collect(auto_candidates(sources), sources, folder, keywords)[:AUTO_IMAGES]
            if images:
                content = place_images(content, [i.id for i in images], [i.caption for i in images])
                warnings.append(f"원고에 쓸 인용 이미지가 없어 공식 출처 본문의 관련 그림 {len(images)}개를 넣었습니다")
            else:
                warnings.append("본문에 넣을 이미지가 없습니다 (원고 후보도, 공식 출처 본문의 관련 그림도 없음)")
        topic = Topic(title=files.topic, category=content.category, angle="", keywords=(files.topic,),
                      source_url=files.source_url)
        sheet = FactSheet(topic, "", (), sources, images=images)
        content = attach_references(content, sheet, now)
        cover = self._cover(folder / "cover.png", content.title, content.category, self._blog_name, f"{now:%Y.%m.%d}")
        history = self._history(strict=False)  # '함께 보면 좋은 글' 링크용 (못 읽으면 링크만 빠짐)
        ctx = PublishContext(content, cover, base_notes(now, True), history, now.strftime("%Y%m%d_%H%M"), images,
                             files.topic, files.source_url, tuple(s.url for s in sources))
        preview = write_preview(ctx, folder, dry_run=True)
        html = build_publish_html(self._host, ctx)
        labels = tuple(build_labels(content.category, content.hashtags))
        (folder / "publish.html").write_text(html, encoding="utf-8")
        line = fact_line(files.factcheck)
        sources_md = folder / "sources.md"
        sources_md.write_text(sources_markdown(files, sources, line), encoding="utf-8")
        return PreparedDraft(draft_id, content, html, labels, preview, cover, files.topic, files.source_url,
                             line, tuple(warnings), sources_md, tuple(s.url for s in files.sources))


def build_draft_blocks(prepared: PreparedDraft, request: Mapping[str, str] | None = None) -> list[dict]:
    manual = " · 🧑‍💼 수동 지시" if request else ""
    body = (f"*제목:* {escape_mrkdwn(prepared.content.title)}\n*카테고리:* {escape_mrkdwn(prepared.content.category)}\n"
            f"*본문:* {prepared.content.text_length():,}자 · 소제목 {len(prepared.content.sections)}개\n"
            f"*사실 검증:* {escape_mrkdwn(prepared.fact_line)}")
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📝 {LABEL} 발행 대기 원고{manual}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    if prepared.warnings:
        notes = "\n".join(f"• {escape_mrkdwn(w)}" for w in prepared.warnings)[:2000]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*참고*\n{notes}"}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": (
        f"✅ 반응을 누르면 구글 블로그에 발행됩니다 · 미리보기는 스레드의 HTML 파일 · ID `{prepared.draft_id}`")}]})
    return blocks


def send_for_approval(prepared: PreparedDraft, store, slack, channel: str, now: datetime,
                      request: Mapping[str, str] | None = None) -> str:
    """원고를 보관함에 맡기고 슬랙에 승인 요청을 올린다 — 슬랙 메시지 위치를 원고에 연결해 둔다.

    request = 수동 지시 표시 {"id","kind"} — 카드에 '🧑‍💼 수동 지시'를 붙이고 보관함 기록에 남긴다 (발행 결과를 지시 스레드에 알림).
    """
    bundle = {**prepared.bundle(now), **({"request": dict(request)} if request else {})}
    store.put(prepared.draft_id, bundle)
    manual = "🧑‍💼 수동 지시 · " if request else ""
    ts = slack.post(f"{manual}📝 {LABEL} 발행 대기 원고 — {prepared.content.title}",
                    build_draft_blocks(prepared, request), channel=channel)
    if not ts:
        raise DraftError("슬랙 메시지를 보내지 못했습니다 (봇 토큰·채널 확인)")
    if prepared.preview is not None:
        slack.upload_file(prepared.preview, "구글 블로그 미리보기", "👀 올라갈 모습 — 내려받아 브라우저로 열어 보세요",
                          thread_ts=ts, channel=channel)
    slack.upload_file(prepared.cover, "대표 이미지", "🖼️ 대표 이미지", thread_ts=ts, channel=channel)
    if prepared.sources_md is not None:
        slack.upload_file(prepared.sources_md, "출처·사실 검증", "🔎 출처와 사실 검증 결과", thread_ts=ts, channel=channel)
    store.put(prepared.draft_id, {**bundle, "slack": {"channel": channel, "ts": ts}})
    return ts
