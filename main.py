"""포스팅 실행 진입점.

    python main.py                          # 최신 AI 소식에서 주제를 골라 포스팅
    python main.py --topic "Gemini 3.8 Live" # 주제 지정
    python main.py --dry-run                # 발행 없이 조사·원고·사실 검증·미리보기만 슬랙으로
    python main.py --mode setup             # 초기 세팅만 실행 (= python setup.py)
"""
import sys

from autopost.cli import main

if __name__ == "__main__":
    sys.exit(main())
