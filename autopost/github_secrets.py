"""GitHub Actions 실행 중 갱신된 토큰/세션을 리포지토리 시크릿에 다시 저장.

GH_SECRETS_TOKEN (Secrets: Read and write 권한의 fine-grained PAT) 이 있을 때만 동작한다.
값은 표준입력으로 전달해 프로세스 목록/로그에 노출되지 않게 한다.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Mapping

log = logging.getLogger(__name__)


def update_github_secret(name: str, value: str, env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    token = source.get("GH_SECRETS_TOKEN", "").strip()
    repo = source.get("GITHUB_REPOSITORY", "").strip()
    gh = shutil.which("gh")
    if not (token and repo and gh and value):
        return False
    try:
        result = subprocess.run(
            [gh, "secret", "set", name, "--repo", repo],
            input=value,
            text=True,
            capture_output=True,
            env={**os.environ, "GH_TOKEN": token},
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("GitHub 시크릿 %s 업데이트 실패: %s", name, exc)
        return False
    if result.returncode != 0:
        log.error("GitHub 시크릿 %s 업데이트 실패 (exit %d)", name, result.returncode)
        return False
    log.info("GitHub 시크릿 %s 업데이트 완료", name)
    return True
