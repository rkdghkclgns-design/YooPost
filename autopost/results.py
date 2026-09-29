"""플랫폼 발행 결과 공용 타입."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PublishResult:
    platform: str
    ok: bool
    url: str = ""
    message: str = ""
    skipped: bool = False

    @classmethod
    def skip(cls, platform: str, reason: str) -> "PublishResult":
        return cls(platform=platform, ok=False, message=reason, skipped=True)

    @classmethod
    def fail(cls, platform: str, error: Exception | str) -> "PublishResult":
        return cls(platform=platform, ok=False, message=str(error)[:500])
