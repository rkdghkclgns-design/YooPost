"""클라우드 루틴(Claude)이 쓴 원고를 깃 브랜치로 받아, GitHub Actions 가 슬랙 승인 요청으로 보낸다.

클라우드 세션은 슬랙·이미지 저장소·구글 블로그에 접속하지 못하고 비밀값도 없다 (웹 조사와 git push 만).
    클라우드: 원고 폴더(post.json·research.json)를 yoopost-drafts 브랜치 drafts/<ID>/ 에 올린다
    Actions(cloud_drafts.yml): push 를 받으면 아직 보내지 않은 원고를 준비해 슬랙 승인 요청을 보내고,
        진행 막대를 고치고, 발행 기록(state/published.json)을 새로 쓴다 (비밀값은 GitHub 에만 있다)

yoopost-drafts 브랜치 (코드 없이 데이터만)
    drafts/<YYYYMMDD_HHMMSS_n>/post.json·research.json   원고 (n = 카테고리 번호, 폴더 이름 = 원고 ID)
    drafts/<YYYYMMDD_HHMMSS_n>/skip.json                  건너뛴 카테고리 {"category", "reason"}
    runs/<YYYYMMDD>.json                                  클라우드 실행 기록 {"started", "finished", "network"}
    runs/requests/<요청 ID>.json                         수동 지시 실행 기록 (draft_request.py) — 매일 기록과 따로
    state/progress_<YYYYMMDD>.json                        진행 메시지 ts·요약 전송 여부 (Actions 가 씀)
    state/failures.json                                   전송 실패 {원고 ID: {"hash", "reason"}} (Actions 가 씀)
    state/published.json                                  발행 기록 — 클라우드가 주제 중복을 피할 때 읽음
    state/requests.json                                   수동 지시 스레드에 이미 알린 것 (Actions 가 씀)

수동 지시 원고(research.json·skip.json 의 "request")는 여느 원고처럼 슬랙 승인 요청으로 보내되, 매일 7개 카테고리의
빈칸 계산·진행 막대에는 넣지 않는다 (07시 전에 지시해도 07시 루틴이 카테고리를 건너뛰지 않도록).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Sequence

from .claude_draft import DraftError, folder_category, load_draft
from .constants import CATEGORIES
from .draft_request import read_request, request_id_of
from .freshness import folder_date, freshness_problems
from .history import MAX_ENTRIES, HistoryEntry, PostHistory, combined, merge_published

log = logging.getLogger(__name__)

BRANCH = "yoopost-drafts"
WORKTREE = ".yoopost-drafts"
DRAFTS, RUNS, STATE = "drafts", "runs", "state"
SKIP_FILE = "skip.json"
FOLDER_RE = re.compile(r"^(\d{8})_(\d{6})_(\d+)$")
EMOJI_HEADING = re.compile(r"^[^\w\s]")  # 이모지·기호로 시작하는 소제목
MIN_EMOJI_HEADINGS, MIN_FAQ, MIN_TABLES = 5, 3, 2
MAX_REASON = 300
MAX_ATTEMPTS = 3  # 일시적 오류(슬랙·보관함·이미지 서버)는 실행마다 다시 시도


def folder_day(name: str) -> str:
    """원고 폴더 이름의 날짜(YYYYMMDD) — 형식이 아니면 빈 문자열."""
    match = FOLDER_RE.match(name)
    return match.group(1) if match else ""


def recent_days(now: datetime, count: int = 2) -> tuple[str, ...]:
    return tuple((now - timedelta(days=i)).strftime("%Y%m%d") for i in range(count))


def draft_folders(drafts_dir: Path, days: Iterable[str]) -> list[Path]:
    wanted = set(days)
    if not drafts_dir.is_dir():
        return []
    return sorted(p for p in drafts_dir.iterdir()
                  if p.is_dir() and not p.is_symlink() and folder_day(p.name) in wanted)


def pushed_names(tree: Path) -> set[str]:
    """원격 원고 브랜치에 올라간 원고 폴더 이름 — 로컬에만 있는(올리지 못한) 원고는 치지 않는다."""
    if not (tree / ".git").exists():
        return set()
    try:
        listing = subprocess.run(["git", "ls-tree", "-r", "--name-only", f"origin/{BRANCH}", "--", f"{DRAFTS}/"],
                                 cwd=tree, capture_output=True, text=True, encoding="utf-8")
    except OSError:
        return set()
    names = set()
    for line in listing.stdout.splitlines():
        parts = line.strip().split("/")
        if len(parts) == 3 and parts[2] in ("post.json", "research.json", SKIP_FILE):
            names.add(parts[1])
    return names


def has_symlink(folder: Path) -> bool:
    """브랜치에서 받은 폴더에 심볼릭 링크가 있으면 쓰지 않는다 (밖의 파일을 읽거나 덮어쓰지 않도록)."""
    return folder.is_symlink() or any(p.is_symlink() for p in folder.iterdir())


def is_skip(folder: Path) -> bool:
    return (folder / SKIP_FILE).is_file()


def fingerprint(folder: Path) -> str:
    """원고 내용 지문 — 실패한 원고를 고쳐 다시 올리면 지문이 바뀌어 다시 보낸다."""
    digest = hashlib.sha256()
    for name in ("post.json", "research.json"):
        path = folder / name  # Windows 작업 폴더(CRLF)에서도 Actions(LF)와 같은 지문이 되게 줄바꿈을 맞춘다
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") if path.is_file() else b"")
    return digest.hexdigest()[:16]


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ 빈 카테고리 (건너뜀·전송 실패·원고 없음) → 자동 보충 대상
@dataclass(frozen=True)
class Gap:
    number: int  # 카테고리 번호 (1~7, 블로그 메뉴 순서)
    category: str
    reason: str


def is_complete(folder: Path) -> bool:
    """사실 검증까지 끝난 원고 (post.json·research.json, factcheck 에 claims·confirmed)."""
    if is_skip(folder) or not (folder / "post.json").is_file():
        return False
    fc = read_json(folder / "research.json").get("factcheck")
    return isinstance(fc, dict) and isinstance(fc.get("claims"), int) and isinstance(fc.get("confirmed"), int)


def missing_categories(drafts_dir: Path, day: str, categories: Sequence[str],
                       failures: Mapping[str, Mapping[str, object]] | None = None,
                       pushed: Iterable[str] | None = None, request: str = "",
                       days: Iterable[str] | None = None) -> list[Gap]:
    """오늘(YYYYMMDD) 쓸 만한 원고가 없는 카테고리 — 건너뛴 것, 보내다 멈춘 것, 올리지 못한 것, 아예 없는 것.

    같은 카테고리에 쓸 만한 원고가 하나라도 있으면 빈 카테고리가 아니다 (보충한 원고가 건너뜀 표시를 이긴다).
    쓸 만한 원고 = 사실 검증까지 끝났고, 원고 브랜치에 올라갔고(pushed 가 주어지면), Actions 가 더 보내지 않기로
    멈춘(parked) 실패가 아닌 것 — 전송 재시도 규칙(sync_drafts)과 같은 기준.
    request 가 비어 있으면 매일 원고만, 요청 ID 면 그 수동 지시 원고만 본다 (days = 볼 날짜들, 기본은 day 하루).
    categories 는 늘 전체 목록을 넘긴다 — 번호(폴더 이름 끝)가 목록 순서다.
    """
    failures = failures or {}
    remote = None if pushed is None else set(pushed)
    folders = [f for f in draft_folders(drafts_dir, list(days) if days is not None else [day])
               if request_id_of(f) == request]
    gaps: list[Gap] = []
    for number, category in enumerate(categories, start=1):
        mine = [f for f in folders if f.name.endswith(f"_{number}")]
        complete = [f for f in mine if is_complete(f)]
        on_branch = [f for f in complete if remote is None or f.name in remote]
        parked = [f for f in on_branch if _parked(failures.get(f.name), fingerprint(f))]
        if len(on_branch) > len(parked):
            continue
        skipped = [str(read_json(f / SKIP_FILE).get("reason") or "") for f in mine if is_skip(f)]
        if parked:
            reason = f"전송 실패(재시도 중단): {failures[parked[-1].name].get('reason') or ''}"
        elif complete:
            reason = "원고를 브랜치에 올리지 못함 (로컬에만 있음)"
        elif skipped:
            reason = f"건너뜀: {skipped[-1] or '이유 없음'}"
        else:
            reason = "원고 없음"
        gaps.append(Gap(number, category, reason[:MAX_REASON]))
    return gaps


# ------------------------------------------------------------------ 품질 확인 (클라우드에서, 네트워크 없이)
@dataclass(frozen=True)
class QualityReport:
    problems: tuple[str, ...]
    stats: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems


def quality_report(folder: Path, min_chars: int) -> QualityReport:
    """원고 폴더가 올릴 만한지 — 형식·분량·구성·사실 검증 기록을 확인한다 (출처·이미지 접속 확인은 Actions 가)."""
    try:
        files = load_draft(folder)
    except DraftError as exc:
        return QualityReport((str(exc),))
    content = files.content
    blocks = [b for s in content.sections for b in s.blocks]
    length = content.text_length()
    emoji = sum(1 for s in content.sections if EMOJI_HEADING.match(s.heading))
    tables = sum(1 for b in blocks if b.kind == "table")
    boxes = sum(1 for b in blocks if b.kind in ("info", "warning"))
    paragraphs = [b.text for b in blocks if b.kind == "paragraph"]
    highlighted = sum(1 for t in paragraphs if "**" in t)
    official = sum(1 for s in files.sources if s.official)
    image_ids = {b.image_id for b in blocks if b.kind == "image"}
    known = {c.id for c in files.candidates}
    fc = files.factcheck
    problems = []
    if length < min_chars:
        problems.append(f"본문이 {length:,}자입니다 (최소 {min_chars:,}자)")
    if emoji < MIN_EMOJI_HEADINGS:
        problems.append(f"이모지 소제목이 {emoji}개입니다 ({MIN_EMOJI_HEADINGS}개 이상)")
    if len(content.faq) < MIN_FAQ:
        problems.append(f"FAQ 가 {len(content.faq)}개입니다 (4개 안팎)")
    if tables < MIN_TABLES:
        problems.append(f"표가 {tables}개입니다 ({MIN_TABLES}개 이상)")
    if boxes == 0:
        problems.append("💡/⚠️ 박스(info·warning)가 없습니다")
    if paragraphs and highlighted * 2 < len(paragraphs):
        problems.append(f"형광 강조(**…**)가 있는 문단이 {highlighted}/{len(paragraphs)}개뿐입니다")
    if official == 0:
        problems.append("공식 출처(official: true)가 없습니다")
    if image_ids - known:
        problems.append(f"research.json images 에 없는 이미지 블록: {', '.join(sorted(image_ids - known))}")
    none_reason = str(read_json(folder / "research.json").get("images_none_reason") or "").strip()
    if not files.candidates and not none_reason:
        problems.append("인용 이미지 후보(research.json images)가 없습니다 — 공식 출처 본문의 관련 화면·도표 1~3개(썸네일 제외)를 "
                        "넣고 본문에 image 블록을 두세요 (정말 없으면 images_none_reason 에 이유)")
    elif files.candidates and not image_ids:
        problems.append("인용 이미지 후보는 있는데 본문에 image 블록이 없습니다 — 관련 문단 뒤에 {\"type\": \"image\", \"image_id\": \"I1\"}")
    if not (isinstance(fc.get("claims"), int) and isinstance(fc.get("confirmed"), int)):
        problems.append("research.json factcheck(claims·confirmed)가 비어 있습니다 — 사실 검증이 끝나지 않았습니다")
    stats = (f"본문 {length:,}자 · 소제목 {len(content.sections)}개(이모지 {emoji}) · 표 {tables} · 박스 {boxes} · "
             f"FAQ {len(content.faq)} · 강조 문단 {highlighted}/{len(paragraphs)} · 출처 {len(files.sources)}"
             f"(공식 {official}) · 이미지 {len(known)}")
    return QualityReport(tuple(problems), stats)


# ------------------------------------------------------------------ 전송 (Actions 에서)
@dataclass(frozen=True)
class SyncOutcome:
    sent: tuple[str, ...] = ()
    already: tuple[str, ...] = ()
    new_failures: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    unknown: tuple[str, ...] = ()  # 보관함을 확인하지 못해 다음 실행으로 미룬 원고
    messages: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))  # 보낸 원고 → 슬랙 메시지 ts


def delivered(record: Mapping[str, object] | None) -> bool:
    """슬랙 승인 요청까지 끝난 원고 — 보관함 기록에 슬랙 메시지 위치가 있어야 ✅ 가 원고와 연결된다."""
    slack = (record or {}).get("slack")
    return isinstance(slack, Mapping) and bool(slack.get("ts"))


def _parked(record: Mapping[str, object] | None, mark: str) -> bool:
    """같은 내용으로 더 시도하지 않을 실패 — 원고 문제(영구)이거나 일시적 오류를 여러 번 겪음."""
    if not record or record.get("hash") != mark:
        return False
    return bool(record.get("permanent")) or int(record.get("attempts") or 0) >= MAX_ATTEMPTS


def is_parked(folder: Path, failures: Mapping[str, Mapping[str, object]]) -> bool:
    """Actions 가 더 보내지 않기로 멈춘 원고 (원고 문제·여러 번 실패) — 브랜치에 있어도 쓸 만한 원고가 아니다."""
    return _parked(failures.get(folder.name), fingerprint(folder))


def sync_drafts(folders: Sequence[Path], lookup: Callable[[str], Mapping | None], send: Callable[[Path], str | None],
                failures: Mapping[str, Mapping[str, object]], clean: Callable[[str], str] = lambda t: t,
                keep_days: Iterable[str] = ()) -> tuple[SyncOutcome, dict]:
    """슬랙 승인 요청이 끝나지 않은 원고만 보낸다. (결과, 새 실패 기록)

    lookup(원고 ID) = 보관함 기록 (없으면 None, 확인 못 하면 예외). 원고 문제(DraftError)는 내용이 바뀔 때까지 다시
    보내지 않고, 일시적 오류는 MAX_ATTEMPTS 번까지 실행마다 다시 시도한다. 알림은 같은 내용의 첫 실패 때만.
    """
    sent, already, unknown = [], [], []
    new_failures: dict[str, str] = {}
    messages: dict[str, str] = {}
    names = {f.name for f in folders}
    keep = set(keep_days)  # 멈춘 원고 기록은 겹침 비교 기간 동안 남긴다 (거절된 원고가 '다른 원고'로 되살아나지 않게)
    records = {k: dict(v) for k, v in failures.items() if k in names or folder_day(k) in keep}
    for folder in folders:
        draft_id = folder.name
        if is_skip(folder) or not (folder / "post.json").is_file() or not (folder / "research.json").is_file():
            continue
        mark = fingerprint(folder)
        previous = records.get(draft_id) if records.get(draft_id, {}).get("hash") == mark else None
        if _parked(previous, mark):
            continue
        try:
            if delivered(lookup(draft_id)):
                already.append(draft_id)
                records.pop(draft_id, None)
                continue
        except Exception as exc:
            log.warning("보관함 확인 실패 %s: %s", draft_id, exc)
            unknown.append(draft_id)
            continue
        try:
            if has_symlink(folder):
                raise DraftError("원고 폴더에 심볼릭 링크가 있어 쓰지 않습니다")
            ts = send(folder)
        except Exception as exc:
            reason = clean(str(exc))[:MAX_REASON] or type(exc).__name__
            attempts = int((previous or {}).get("attempts") or 0) + 1
            log.warning("원고 전송 실패 %s (%d번째): %s", draft_id, attempts, reason)
            records[draft_id] = {"hash": mark, "reason": reason, "attempts": attempts,
                                 "permanent": isinstance(exc, DraftError)}
            if attempts == 1:
                new_failures[draft_id] = reason
            continue
        sent.append(draft_id)
        messages[draft_id] = str(ts or "")
        records.pop(draft_id, None)
    outcome = SyncOutcome(tuple(sent), tuple(already), MappingProxyType(new_failures), tuple(unknown),
                          MappingProxyType(messages))
    return outcome, records


def failed_reasons(records: Mapping[str, Mapping[str, object]]) -> dict[str, str]:
    return {k: str(v.get("reason") or "") for k, v in records.items()}


def run_active(run: Mapping[str, object]) -> bool:
    """클라우드 실행이 진행 중인지 (시작했고 아직 끝나지 않음)."""
    return bool(run.get("started")) and not run.get("finished")


PENDING_DAYS = 30  # 아직 발행 전일 수 있는 원고와 겹침을 비교하는 기간 (최신 이슈 기간보다 넉넉히)


def draft_entry(folder: Path) -> HistoryEntry | None:
    """원고 폴더 → 겹침 비교용 기록 (주제·제목·대표 원문·출처 주소 전부)."""
    research, post = read_json(folder / "research.json"), read_json(folder / "post.json")
    if not research or not post:
        return None
    day = folder_day(folder.name)
    urls = tuple(str(s.get("url") or "") for s in research.get("sources") or []
                 if isinstance(s, Mapping) and s.get("url"))
    return HistoryEntry(f"{day[:4]}-{day[4:6]}-{day[6:]}" if day else "", str(post.get("title") or ""),
                        str(research.get("topic") or ""), str(post.get("category") or ""),
                        str(research.get("source_url") or ""), sources=urls, where=f"최근 원고 {folder.name}")


def pending_entries(drafts_dir: Path, now: datetime, failures: Mapping[str, Mapping[str, object]],
                    exclude: str = "", before: str = "", include: set[str] | None = None,
                    days: int = PENDING_DAYS) -> tuple[HistoryEntry, ...]:
    """다른 원고들 (✅ 전이라 블로그에 없어도 같은 소식을 또 쓰지 않도록).

    건너뜀·Actions 가 멈춘 원고(중복·오래된 소식 등)는 빼서 그 카테고리를 다시 쓸 수 있게 한다.
    before = 이 이름보다 먼저 쓴 원고만 (Actions 는 먼저 쓴 원고를 살린다), include = 이 이름들만 (원격에 올라간 원고).
    """
    found = []
    for other in draft_folders(drafts_dir, recent_days(now, days)):
        if other.name == exclude or (before and other.name >= before) or is_skip(other) or is_parked(other, failures):
            continue
        if include is not None and other.name not in include:
            continue
        entry = draft_entry(other)
        if entry is not None:
            found.append(entry)
    return tuple(found)


def user_topic(tree: Path, folder: Path, rid: str = "") -> bool:
    """사용자가 슬랙에서 주제를 정해 지시한 원고(수동 지시 single + 주제) — 최신 이슈 조건을 적용하지 않는다."""
    record = read_request(tree, rid or request_id_of(folder))
    return str(record.get("kind") or "") == "single" and bool(str(record.get("topic") or "").strip())


def published_snapshot(tree: Path) -> tuple[HistoryEntry, ...]:
    """Actions 가 남긴 발행 기록 (state/published.json) — 클라우드 세션은 블로그에 접속하지 못한다."""
    try:
        raw = json.loads((tree / STATE / "published.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ()
    return tuple(HistoryEntry.from_dict(e) for e in raw if isinstance(e, dict)) if isinstance(raw, list) else ()


def novelty_problems(folder: Path, tree: Path, now: datetime, include: set[str] | None = None,
                     rid: str = "") -> list[str]:
    """올리기 전에 — 이미 쓴 글·다른 원고와 겹치는지, 최신 이슈인지 (Actions 가 한 번 더 확인한다)."""
    mine = draft_entry(folder)
    if mine is None:
        return ["post.json·research.json 을 읽지 못해 겹침을 확인할 수 없습니다"]
    problems = []
    numbered = folder_category(folder.name)
    if numbered and mine.category != numbered:
        problems.append(f"post.json category '{mine.category}' 가 원고 폴더 번호의 카테고리 '{numbered}' 와 다릅니다 "
                        "(카테고리를 바꿔 기간을 늘리지 않는다)")
    others = pending_entries(tree / DRAFTS, now, read_json(tree / STATE / "failures.json"), exclude=folder.name,
                             include=include)
    found = combined(PostHistory(published_snapshot(tree)), others).find_duplicate(
        mine.topic, mine.source_url, mine.title, mine.sources)
    if found:
        entry, reason = found
        problems.append(f"이미 쓴 글과 겹칩니다 — {entry.where or '발행한 글'} '{entry.title}' ({reason}) — 다른 소식으로 바꾸세요")
    if not user_topic(tree, folder, rid):
        try:
            files = load_draft(folder)
        except DraftError:
            return problems  # 형식 오류는 quality_report 가 알린다
        problems += freshness_problems(files.content.category, files.sources, files.source_url,
                                       folder_date(folder.name, now.date()))
    return problems


def sync_and_report(root: Path, now: Callable[[], datetime], store, send: Callable[[Path], str | None], slack,
                    load_entries: Callable[[], Sequence], clean: Callable[[str], str] = lambda t: t,
                    manual_channel: str = "", draft_channel: str = "") -> SyncOutcome:
    """Actions 한 번 실행분 — 새 원고 전송 → 실패 알림 → 수동 지시 스레드 → 진행 막대 → (끝났으면) 요약 → 발행 기록 파일."""
    from .draft_progress import ProgressBoard
    from .request_report import report_requests
    from .slack_helper import escape_mrkdwn

    current = now()
    failures_file = root / STATE / "failures.json"
    folders = draft_folders(root / DRAFTS, recent_days(current))
    outcome, records = sync_drafts(folders, store.find, send, read_json(failures_file), clean,
                                   keep_days=recent_days(current, PENDING_DAYS))
    write_json(failures_file, records)
    for draft_id, reason in outcome.new_failures.items():
        slack.post(f"⚠️ 클라우드 원고를 슬랙 승인 요청으로 보내지 못했습니다 — `{draft_id}`: {escape_mrkdwn(reason)}")
    try:
        report_requests(root, folders, outcome, records, slack, current, manual_channel, draft_channel,
                        lookup=store.find)
    except Exception as exc:  # 수동 지시 알림이 원고 전송·진행 막대를 멈추지 않게
        log.warning("수동 지시 스레드에 알리지 못했습니다: %s", exc)
    day = current.strftime("%Y%m%d")
    run = read_json(root / RUNS / f"{day}.json")
    progress = root / STATE / f"progress_{day}.json"
    if run or any(folder_day(f.name) == day and not request_id_of(f) for f in folders):
        board = ProgressBoard(slack, root / DRAFTS, progress, store, now=now)
        rows = board.refresh(failed_reasons(records), run_active(run))
        if run.get("finished") and not read_json(progress).get("summary"):
            slack.post(summary_text(rows, current.strftime("%m/%d"), str(run.get("network") or "")))
            write_json(progress, {**read_json(progress), "summary": current.isoformat(timespec="seconds")})
    try:
        entries = merge_published(published_snapshot(root), list(load_entries()))
        write_json(root / STATE / "published.json", [e.to_dict() for e in entries])
    except Exception as exc:
        log.warning("발행 기록을 새로 쓰지 못했습니다: %s", exc)
    return outcome


AUTOFILL_HINT = ("🔁 빈 카테고리는 PC가 켜져 있으면 08:30·12:30·17:30 에 자동으로 보충됩니다 "
                 "(로컬 예약 작업 'yoopost-daily-category-drafts')")


def watchdog(root: Path, now: datetime, slack) -> bool:
    """오늘 클라우드 실행이 없거나(루틴이 돌지 않음) 끝나지 않았거나, 빈 카테고리가 남았으면 알린다. 정상이면 True."""
    run = read_json(root / RUNS / f"{now:%Y%m%d}.json")
    if not run or run_active(run):
        problem = "시작했지만 끝나지 않았습니다" if run else "실행 기록이 없습니다"
        slack.post(f"⚠️ {now:%m/%d} 클라우드 원고 루틴이 {problem} — "
                   f"claude.ai/code/routines 에서 'YooPost 매일 원고' 실행 결과를 확인해 주세요\n{AUTOFILL_HINT}")
        return False
    gaps = missing_categories(root / DRAFTS, f"{now:%Y%m%d}", CATEGORIES, read_json(root / STATE / "failures.json"))
    if not gaps:
        return True
    lines = "\n".join(f"• {g.category} — {g.reason}" for g in gaps)
    slack.post(f"⏭️ {now:%m/%d} 아직 원고가 없는 카테고리 {len(gaps)}개\n{lines}\n{AUTOFILL_HINT}")
    return False


NETWORK_HINT = ("⚠️ 클라우드 네트워크가 제한되어 조사할 수 있는 사이트가 적었습니다 — "
                "claude.ai/code 의 환경 설정에서 네트워크 액세스를 '전체'로 바꾸면 조사 품질이 올라갑니다")


def summary_text(rows: Sequence, day_label: str, network: str = "") -> str:
    """실행이 끝났을 때 #yoopost-알림 에 보내는 요약 (rows = 진행 막대의 카테고리별 줄)."""
    waiting = sum(1 for r in rows if r.icon == "📝")
    lines = [f"📚 {day_label} 클라우드 원고 작성 결과 — 승인 대기 {waiting}편 (#yoopost-1-원고승인 에서 ✅)"]
    lines += [f"{r.icon} {r.category} — {r.text}" for r in rows]
    if any(r.icon in ("⏭️", "⚠️") for r in rows):
        lines.append(AUTOFILL_HINT)
    if network == "limited":
        lines.append(NETWORK_HINT)
    return "\n".join(lines)
