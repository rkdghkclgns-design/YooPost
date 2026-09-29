"""1회용 설정 스크립트 공용: 값을 로컬 .env 와 GitHub 시크릿(gh CLI)에 저장한다.

값은 화면·명령줄 인자에 남기지 않고 gh 의 표준입력으로만 전달한다.
시크릿은 저장소가 아니라 production 환경(main 브랜치의 워크플로만 읽음)에 넣는다 —
다른 브랜치에 워크플로 파일을 올려도 시크릿을 꺼낼 수 없게. 저장소 시크릿이 따로 있어도 환경 값이 우선한다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Mapping

from .envfile import update_env_file

Runner = Callable[..., subprocess.CompletedProcess]

SECRETS_ENVIRONMENT = "production"


def upload_secrets(values: Mapping[str, str], run: Runner = subprocess.run) -> list[str]:
    """gh CLI 로 GitHub 시크릿(production 환경) 등록. 실패한 이름 목록을 돌려준다."""
    failed = []
    for name, value in values.items():
        try:
            result = run(["gh", "secret", "set", "--env", SECRETS_ENVIRONMENT, name],
                         input=value, text=True, capture_output=True)
        except FileNotFoundError:  # gh CLI 미설치
            return list(values)
        if result.returncode != 0:
            failed.append(name)
    return failed


def upload_variables(values: Mapping[str, str], run: Runner = subprocess.run) -> list[str]:
    """gh CLI 로 GitHub Actions Variables(비밀이 아닌 설정) 등록. 실패한 이름 목록을 돌려준다."""
    failed = []
    for name, value in values.items():
        try:
            result = run(["gh", "variable", "set", name], input=value, text=True, capture_output=True)
        except FileNotFoundError:  # gh CLI 미설치
            return list(values)
        if result.returncode != 0:
            failed.append(name)
    return failed


def save_everywhere(
    values: Mapping[str, str], env_path: Path, upload: bool = True, run: Runner = subprocess.run
) -> list[str]:
    """.env 갱신 후(항상) GitHub 시크릿 등록. GitHub 등록에 실패한 이름 목록을 돌려준다."""
    update_env_file(env_path, values)
    return upload_secrets(values, run) if upload else []
