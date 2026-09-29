"""Zero-Touch 초기 세팅 진입점 (패키징용 setup.py 가 아닙니다).

    python setup.py

구글 드라이브 폴더 구조·사용 안내·발행 기록 파일을 검사하고, 없으면 자동 생성한 뒤
슬랙으로 "초기 세팅 완료" 와 연동 점검 결과를 보고한다. 여러 번 실행해도 안전하다.
"""
import sys

from autopost.cli import main

if __name__ == "__main__":
    sys.exit(main(["--mode", "setup"]))
