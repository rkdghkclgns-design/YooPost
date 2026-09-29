"""블로그 이름·글 양식·뉴스 소스 등 시스템 전역 상수."""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_BLOG_NAME = "AI백과사전"

# 참고 블로그(fornewchallenge.tistory.com)의 카테고리 체계 — 구글 블로그 상단 메뉴 순서와 같다
#   홈 | AI 언어 모델 | AI 도구 | AI 논문 분석 | AI 뉴스 리뷰 | AI 보안 | AI 인사이트 | AI 공모주 분석
# 글마다 카테고리를 첫 번째 라벨로 붙이므로 메뉴는 라벨 페이지(/search/label/카테고리)로 연결된다.
CATEGORIES: tuple[str, ...] = (
    "AI 언어 모델",
    "AI 도구",
    "AI 논문 분석",
    "AI 뉴스 리뷰",
    "AI 보안",
    "AI 인사이트",
    "AI 공모주 분석",
)
DEFAULT_CATEGORY = "AI 뉴스 리뷰"
HOME_MENU = "홈"
CATEGORY_HINTS = {
    "AI 언어 모델": "새 모델 출시·업데이트 (스펙·가격·벤치마크·사용법)",
    "AI 도구": "AI 앱·에이전트·개발 도구 (설치·사용 가이드·활용 시나리오)",
    "AI 논문 분석": "주목받는 AI 논문 (핵심 아이디어·실험 결과·한계)",
    "AI 뉴스 리뷰": "AI 업계 소식 (인수·투자·정책·서비스 출시)",
    "AI 보안": "AI 관련 보안 이슈·취약점·안전 정책",
    "AI 인사이트": "AI 흐름을 짚는 분석 (여러 발표를 엮은 해설)",
    "AI 공모주 분석": "AI 관련 기업 공모주(IPO) — 증권신고서·공시에 나온 사실만 정리 (투자 권유 아님)",
}
# 공모주 글에 반드시 붙는 안내 (투자 판단은 독자 몫 — 수익 전망·매수 권유를 쓰지 않는다)
IPO_DISCLAIMER = "이 글은 공시 자료를 정리한 정보 제공용이며 투자 권유가 아닙니다. 투자 판단과 책임은 투자자 본인에게 있습니다."

# 글 양식 — 참고 블로그 전체 게시물 분석(autopost/data/style_profile.json)에서 가장 많이 쓰인 고정 소제목
SUMMARY_TITLE = "한눈에 보기"
FAQ_HEADING = "❓ 자주 묻는 질문"
RELATED_HEADING = "🔗 함께 보면 좋은 글"
CLOSING_HEADING = "🎯 맺음말"
REFERENCES_HEADING = "📚 참고 문헌 및 출처"
CLOSING_SIGNATURE = "저는 다음 시간에 더 유익한 정보를 가지고 다시 찾아뵙겠습니다. 감사합니다."
BASIS_NOTE = "※ 본 글은 {date} 기준으로 작성되었습니다."
FACT_NOTE = "이 글의 수치·날짜·이름 등 사실 관계는 구글 딥리서치로 조사한 공식 발표·1차 자료를 웹 검색으로 다시 교차 확인했습니다."
RELATED_POSTS = 3


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    kind: str = "rss"  # rss(RSS·Atom) | hf_papers | hn
    official: bool = False  # 회사 공식 발표 채널 (1차 자료)
    ai_only: bool = True  # False 면 AI 키워드가 있는 글만 후보로


FEEDS: tuple[Feed, ...] = (
    Feed("OpenAI", "https://openai.com/news/rss.xml", official=True),
    Feed("Google DeepMind", "https://deepmind.google/blog/rss.xml", official=True),
    Feed("Google AI", "https://blog.google/technology/ai/rss/", official=True),
    Feed("Hugging Face", "https://huggingface.co/blog/feed.xml", official=True),
    Feed("Mistral AI", "https://mistral.ai/rss.xml", official=True),
    Feed("NVIDIA", "https://blogs.nvidia.com/feed/", official=True, ai_only=False),
    Feed("AWS Machine Learning", "https://aws.amazon.com/blogs/machine-learning/feed/", official=True),
    Feed("GitHub Blog AI", "https://github.blog/ai-and-ml/feed/", official=True),
    Feed("AI타임스", "https://www.aitimes.com/rss/allArticle.xml"),
    Feed("GeekNews", "https://news.hada.io/rss/news", ai_only=False),
    Feed("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    Feed("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    Feed("Hugging Face Daily Papers", "https://huggingface.co/api/daily_papers", kind="hf_papers"),
    Feed("Hacker News",
         "https://hn.algolia.com/api/v1/search?tags=story&query=AI&numericFilters=points%3E150",
         kind="hn", ai_only=False),
)

AI_KEYWORDS = (
    "ai", "llm", "gpt", "gemini", "claude", "llama", "qwen", "deepseek", "mistral", "grok",
    "openai", "anthropic", "deepmind", "copilot", "agent", "에이전트", "모델", "인공지능",
    "딥러닝", "머신러닝", "machine learning", "deep learning", "neural", "transformer",
    "diffusion", "생성형", "챗봇", "chatbot", "multimodal", "멀티모달", "rag", "mcp",
)
