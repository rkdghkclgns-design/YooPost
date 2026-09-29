"""원고 보관함 — Claude 가 쓴 '발행 준비 원고'를 Cloudflare Worker(KV)에 맡겨 두고, 슬랙 ✅ 승인 뒤 꺼내 발행한다.

Worker(slack_relay/worker.js)의 PUT/GET /draft/<ID> 는 IMAGE_UPLOAD_TOKEN 이 있어야만 쓰고 읽을 수 있다.
원고에 슬랙 메시지 위치(slack.channel·slack.ts)를 넣어 두면 Worker 가 그 메시지의 ✅ 반응을 원고와 연결한다.
"""
from __future__ import annotations

import json
import logging

from .image_host import TIMEOUT, safe_key

log = logging.getLogger(__name__)


class DraftStoreError(RuntimeError):
    """원고 보관함 읽기·쓰기 실패."""


class DraftStore:
    def __init__(self, base_url: str, token: str, session=None) -> None:
        if session is None:
            import requests

            session = requests.Session()
        self._base = base_url.rstrip("/")
        self._token = token
        self._session = session

    def _url(self, draft_id: str) -> str:
        return f"{self._base}/draft/{safe_key(draft_id)}"

    def put(self, draft_id: str, data: dict) -> None:
        response = self._session.put(
            self._url(draft_id), data=json.dumps(data, ensure_ascii=False).encode("utf-8"), timeout=TIMEOUT,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json; charset=utf-8"},
        )
        if response.status_code not in (200, 201):
            raise DraftStoreError(f"원고 보관 실패 (HTTP {response.status_code}): {draft_id}")

    def get(self, draft_id: str) -> dict:
        response = self._session.get(self._url(draft_id), headers={"Authorization": f"Bearer {self._token}"},
                                     timeout=TIMEOUT)
        if response.status_code != 200:
            raise DraftStoreError(f"원고를 찾지 못했습니다 (HTTP {response.status_code}): {draft_id}")
        data = response.json()
        if not isinstance(data, dict):
            raise DraftStoreError(f"원고 형식이 잘못되었습니다: {draft_id}")
        return data
