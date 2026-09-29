"""Google Drive API v3 래퍼 — 폴더 탐색/생성, 업로드/다운로드/덮어쓰기, 공개 링크."""
from __future__ import annotations

import io
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from .constants import FOLDER_MIME

log = logging.getLogger(__name__)

NUM_RETRIES = 3
FILE_FIELDS = "id, name, mimeType, createdTime"


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    created_time: str = ""

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME


def escape_query_value(value: str) -> str:
    """Drive 검색 쿼리 문자열 리터럴 이스케이프 (역슬래시, 작은따옴표)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_query(parent_id: str, name: str | None = None, mime_type: str | None = None) -> str:
    clauses = [f"'{escape_query_value(parent_id)}' in parents", "trashed = false"]
    if name is not None:
        clauses.append(f"name = '{escape_query_value(name)}'")
    if mime_type is not None:
        clauses.append(f"mimeType = '{escape_query_value(mime_type)}'")
    return " and ".join(clauses)


def _to_file(raw: dict) -> DriveFile:
    return DriveFile(
        id=raw["id"],
        name=raw.get("name", ""),
        mime_type=raw.get("mimeType", ""),
        created_time=raw.get("createdTime", ""),
    )


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def public_image_url(file_id: str, width: int = 1600) -> str:
    """'링크가 있는 모든 사용자' 공개 파일의 이미지 직접 링크."""
    return f"https://lh3.googleusercontent.com/d/{file_id}=w{width}"


class DriveHelper:
    def __init__(self, service) -> None:
        self._service = service

    def _list(self, query: str) -> list[DriveFile]:
        results: list[DriveFile] = []
        page_token: str | None = None
        while True:
            params = {
                "q": query,
                "spaces": "drive",
                "fields": f"nextPageToken, files({FILE_FIELDS})",
                "pageSize": 100,
                "orderBy": "createdTime",
            }
            if page_token:
                params["pageToken"] = page_token
            response = self._service.files().list(**params).execute(num_retries=NUM_RETRIES)
            results.extend(_to_file(item) for item in response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return results

    def find_child(self, parent_id: str, name: str, folder_only: bool = False) -> DriveFile | None:
        matches = self._list(build_query(parent_id, name=name, mime_type=FOLDER_MIME if folder_only else None))
        if len(matches) > 1:
            log.warning("'%s' 이름의 항목이 %d개 있습니다. 가장 오래된 항목을 사용합니다.", name, len(matches))
        return matches[0] if matches else None

    def ensure_folder(self, parent_id: str, name: str) -> tuple[DriveFile, bool]:
        """폴더가 있으면 그대로, 없으면 생성. (폴더, 새로생성여부) 반환."""
        existing = self.find_child(parent_id, name, folder_only=True)
        if existing is not None:
            return existing, False
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        created = self._service.files().create(body=body, fields=FILE_FIELDS).execute(num_retries=NUM_RETRIES)
        log.info("폴더 생성: %s", name)
        return _to_file(created), True

    def upload(self, parent_id: str, local_path: Path, name: str | None = None, mime_type: str | None = None) -> DriveFile:
        guessed = mime_type or mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        media = MediaFileUpload(str(local_path), mimetype=guessed, resumable=True)
        body = {"name": name or local_path.name, "parents": [parent_id]}
        created = (
            self._service.files()
            .create(body=body, media_body=media, fields=FILE_FIELDS)
            .execute(num_retries=NUM_RETRIES)
        )
        return _to_file(created)

    def update_content(self, file_id: str, local_path: Path, mime_type: str | None = None) -> None:
        """같은 파일 ID 의 내용만 바꾼다 (공유 링크·이름 유지)."""
        guessed = mime_type or mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        media = MediaFileUpload(str(local_path), mimetype=guessed, resumable=True)
        self._service.files().update(fileId=file_id, media_body=media, fields="id").execute(num_retries=NUM_RETRIES)

    def download(self, file_id: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        request = self._service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=NUM_RETRIES)
        dest.write_bytes(buffer.getvalue())
        return dest

    def make_public(self, file_id: str) -> None:
        """'링크가 있는 모든 사용자 - 뷰어' 권한 부여 (이미 있으면 무해하게 재적용)."""
        self._service.permissions().create(
            fileId=file_id, body={"type": "anyone", "role": "reader"}, fields="id"
        ).execute(num_retries=NUM_RETRIES)
