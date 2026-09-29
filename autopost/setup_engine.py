"""Zero-Touch 셀프 세팅 엔진.

구글 드라이브의 폴더 구조와 기본 파일(사용 안내·발행 기록)을 검사하고, 없으면 자동 생성한다.
여러 번 실행해도 기존 폴더/파일은 건드리지 않는다(멱등).
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .constants import GUIDE_FILE, GUIDE_TEXT, HISTORY_FILE, PUBLIC_ASSET_FOLDER
from .history import PostHistory

log = logging.getLogger(__name__)

DRIVE_ROOT_ALIAS = "root"


@dataclass(frozen=True)
class DriveLayout:
    root_id: str
    public_folder_id: str


@dataclass(frozen=True)
class SetupReport:
    layout: DriveLayout
    created_folders: tuple[str, ...]
    created_files: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.created_folders or self.created_files)


def _ensure_file(drive, folder_id: str, name: str, text: str, mime: str, workdir: Path, created: list[str]) -> None:
    if drive.find_child(folder_id, name) is not None:
        return
    local = workdir / name
    local.write_text(text, encoding="utf-8")
    drive.upload(folder_id, local, name=name, mime_type=mime)
    created.append(name)
    log.info("기본 파일 생성: %s", name)


def ensure_layout(drive, root_name: str) -> SetupReport:
    created_folders: list[str] = []
    created_files: list[str] = []

    root, is_new = drive.ensure_folder(DRIVE_ROOT_ALIAS, root_name)
    if is_new:
        created_folders.append(root_name)
    public, is_new = drive.ensure_folder(root.id, PUBLIC_ASSET_FOLDER)
    if is_new:
        created_folders.append(f"{root_name}/{PUBLIC_ASSET_FOLDER}")

    with tempfile.TemporaryDirectory(prefix="autopost_setup_") as tmp:
        workdir = Path(tmp)
        _ensure_file(drive, root.id, GUIDE_FILE, GUIDE_TEXT, "text/plain", workdir, created_files)
        _ensure_file(drive, root.id, HISTORY_FILE, PostHistory().to_json(), "application/json", workdir, created_files)

    return SetupReport(DriveLayout(root.id, public.id), tuple(created_folders), tuple(created_files))
