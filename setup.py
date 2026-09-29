"""연동 점검 진입점 (패키징용 setup.py 가 아닙니다).

    python setup.py

구글 블로그·구글 딥리서치(Gemini 키)·이미지 저장소·슬랙을 실제로 연결해 보고 슬랙으로 점검표를 보낸다.
아무것도 만들거나 바꾸지 않으므로 여러 번 실행해도 안전하다. (구글 드라이브는 쓰지 않는다)
"""
import sys

from autopost.cli import main

if __name__ == "__main__":
    sys.exit(main(["--mode", "setup"]))
