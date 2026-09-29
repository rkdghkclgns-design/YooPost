import json

from autopost.style_profile import DEFAULT_HEADINGS, MAX_TARGET_CHARS, PROFILE_PATH, load_style


def test_loads_recent_stats_and_category_headings(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({
        "total_posts": 950,
        "recent": {"median_chars_no_space": 20000, "median_h2": 12, "median_info_boxes": 7.4,
                   "median_faq_questions": 0, "top_title_emoji": [["🤖", 30]],
                   "top_h2_patterns": [["🎯 맺음말", 100], ["📰 등장 배경", 50], ["❓ 자주 묻는 질문", 40]]},
        "recent_by_category": {"AI 도구": {"top_h2_patterns": [["💻 설치·사용 가이드", 9]]}},
    }), encoding="utf-8")
    style = load_style("AI 도구", min_chars=5000, path=path)
    assert style.total_posts == 950 and style.target_chars == MAX_TARGET_CHARS
    assert style.headings == ("💻 설치·사용 가이드", "📰 등장 배경")  # 고정 소제목(맺음말·FAQ)은 제외
    assert style.info_boxes == 7 and style.faq_questions == 6 and style.title_emojis == ("🤖",)


def test_missing_profile_uses_defaults(tmp_path):
    style = load_style("AI 도구", min_chars=9000, path=tmp_path / "none.json")
    assert style.headings == DEFAULT_HEADINGS and style.target_chars == 9000


def test_bundled_profile_is_readable():
    assert PROFILE_PATH.is_file()
    assert load_style("AI 언어 모델").headings
