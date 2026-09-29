"""DriveHelper 를 googleapiclient 흉내 서비스로 검증 (쿼리 이스케이프·페이지 넘김·업로드·덮어쓰기·공개)."""
import io

import pytest

from autopost import drive_helper
from autopost.constants import FOLDER_MIME
from autopost.drive_helper import DriveHelper, build_query, escape_query_value, folder_url, public_image_url


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class FakeFiles:
    def __init__(self, service):
        self.s = service

    def list(self, q, spaces, fields, pageSize, orderBy, pageToken=None):
        self.s.queries.append(q)
        rows = [r for r in self.s.rows if f"'{r['parent']}' in parents" in q and
                (f"name = '{escape_query_value(r['name'])}'" in q or "name =" not in q)]
        start = int(pageToken or 0)
        page = rows[start:start + 1]
        response = {"files": [{k: v for k, v in r.items() if k != "parent"} for r in page]}
        if start + 1 < len(rows):
            response["nextPageToken"] = str(start + 1)
        return _Req(lambda: response)

    def create(self, body, fields, media_body=None):
        def run():
            row = {"id": f"n{len(self.s.rows)}", "name": body["name"], "mimeType": body.get("mimeType", "file"),
                   "parent": body["parents"][0]}
            self.s.rows.append(row)
            return {k: v for k, v in row.items() if k != "parent"}
        return _Req(run)

    def update(self, fileId, media_body, fields):
        return _Req(lambda: self.s.updated.append(fileId))

    def get_media(self, fileId):
        return fileId


class FakePermissions:
    def __init__(self, service):
        self.s = service

    def create(self, fileId, body, fields):
        return _Req(lambda: self.s.public.append((fileId, body)))


class FakeService:
    def __init__(self):
        self.rows, self.queries, self.updated, self.public = [], [], [], []

    def files(self):
        return FakeFiles(self)

    def permissions(self):
        return FakePermissions(self)


class FakeDownloader:
    def __init__(self, buffer: io.BytesIO, request):
        self.buffer, self.request = buffer, request

    def next_chunk(self, num_retries=0):
        self.buffer.write(f"content of {self.request}".encode())
        return None, True


@pytest.fixture
def helper(monkeypatch):
    monkeypatch.setattr(drive_helper, "MediaFileUpload", lambda path, mimetype, resumable: (path, mimetype))
    monkeypatch.setattr(drive_helper, "MediaIoBaseDownload", FakeDownloader)
    service = FakeService()
    return DriveHelper(service), service


def test_query_escaping_and_urls():
    assert escape_query_value("a'b\\c") == "a\\'b\\\\c"
    assert build_query("root", "x", FOLDER_MIME) == (
        f"'root' in parents and trashed = false and name = 'x' and mimeType = '{FOLDER_MIME}'")
    assert folder_url("F") == "https://drive.google.com/drive/folders/F"
    assert public_image_url("I") == "https://lh3.googleusercontent.com/d/I=w1600"


def test_ensure_folder_creates_once_and_pages_through_results(helper):
    drive, service = helper
    folder, created = drive.ensure_folder("root", "AI")
    again, created_again = drive.ensure_folder("root", "AI")
    assert created and not created_again and folder.id == again.id and folder.is_folder
    service.rows.append({"id": "dup", "name": "AI", "mimeType": FOLDER_MIME, "parent": "root"})
    assert drive.find_child("root", "AI", folder_only=True).id == folder.id  # 가장 오래된 항목


def test_upload_update_download_and_public(helper, tmp_path):
    drive, service = helper
    path = tmp_path / "a.json"
    path.write_text("{}")
    uploaded = drive.upload("root", path)
    assert uploaded.name == "a.json"
    drive.update_content(uploaded.id, path)
    assert service.updated == [uploaded.id]
    out = drive.download("file-1", tmp_path / "sub" / "x.bin")
    assert out.read_bytes() == b"content of file-1"
    drive.make_public("file-1")
    assert service.public == [("file-1", {"type": "anyone", "role": "reader"})]
