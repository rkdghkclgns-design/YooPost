"""블로그 이미지 저장소 — 대표 이미지·인용 이미지를 Cloudflare Worker(KV)에 올리고 공개 주소를 받는다.

구글 드라이브를 쓰지 않는다. Worker(slack_relay/worker.js)의 PUT /img/<이름> 은 IMAGE_UPLOAD_TOKEN 으로만
올릴 수 있고, GET /img/<이름> 은 누구나 볼 수 있어 구글 블로그 본문 <img> 주소로 쓴다.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
TIMEOUT = 30
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ImageHostError(RuntimeError):
    """이미지 업로드 실패."""


def safe_key(name: str) -> str:
    """Worker 가 받는 이름 규칙(영문·숫자·._- , 120자)에 맞춘다."""
    key = _UNSAFE.sub("_", name).strip("._-")[:120]
    if not key:
        raise ImageHostError(f"이미지 이름이 비었습니다: {name!r}")
    return key


class ImageHost:
    def __init__(self, base_url: str, token: str, session=None, retries: int = 2,
                 sleep=time.sleep) -> None:
        if session is None:
            import requests

            session = requests.Session()
        self._base = base_url.rstrip("/")
        self._token = token
        self._session = session
        self._retries = retries
        self._sleep = sleep

    def upload(self, path: Path, name: str) -> str:
        """파일을 올리고 공개 주소를 돌려준다 (연결 오류·5xx 는 잠깐 쉬고 다시 시도)."""
        content_type = CONTENT_TYPES.get(path.suffix.lower())
        if content_type is None:
            raise ImageHostError(f"지원하지 않는 이미지 형식: {path.name}")
        key = safe_key(name)
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": content_type}
        data = path.read_bytes()
        last = ""
        for attempt in range(self._retries + 1):
            try:
                response = self._session.put(f"{self._base}/img/{key}", data=data, headers=headers, timeout=TIMEOUT)
            except Exception as exc:  # 네트워크 오류
                last = type(exc).__name__
            else:
                if response.status_code == 201:
                    url = str((response.json() or {}).get("url") or f"{self._base}/img/{key}")
                    log.info("이미지 업로드: %s", url)
                    return url
                last = f"HTTP {response.status_code}"
                if response.status_code < 500:
                    break  # 401·413·415 는 다시 해도 같다
            if attempt < self._retries:
                self._sleep(2 * (attempt + 1))
        raise ImageHostError(f"이미지 저장소 업로드 실패 ({last}): {path.name}")

    def ping(self) -> bool:
        """저장소(Worker)가 응답하는지 — 연동 점검용."""
        try:
            return self._session.get(self._base, timeout=10).status_code == 200
        except Exception:
            return False
