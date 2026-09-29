"""구글 블로그(Blogger API v3) 발행."""
from __future__ import annotations

import logging
from typing import Sequence

from .results import PublishResult

log = logging.getLogger(__name__)

PLATFORM = "구글 블로그"
MAX_LABELS = 6


def build_labels(category: str, hashtags: Sequence[str]) -> list[str]:
    """카테고리를 첫 라벨로, 나머지는 해시태그 앞쪽에서 (Blogger 라벨은 글마다 너무 많으면 지저분해진다)."""
    labels = [category]
    for tag in hashtags:
        label = tag.lstrip("#")
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= MAX_LABELS:
            break
    return labels


class BloggerPublisher:
    def __init__(self, service, blog_id: str) -> None:
        self._service = service
        self._blog_id = blog_id

    def publish(self, title: str, html_body: str, labels: Sequence[str]) -> PublishResult:
        body = {"kind": "blogger#post", "title": title, "content": html_body, "labels": list(labels)}
        response = (
            self._service.posts()
            .insert(blogId=self._blog_id, body=body, isDraft=False)
            .execute(num_retries=3)
        )
        url = response.get("url", "")
        log.info("구글 블로그 발행 완료: %s", url)
        return PublishResult(platform=PLATFORM, ok=True, url=url, message="발행 완료")
