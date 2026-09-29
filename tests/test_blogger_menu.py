from autopost.blog_publisher import build_labels
from autopost.blogger_menu import label_url, menu_links
from autopost.constants import CATEGORIES, IPO_DISCLAIMER
from autopost.content import parse_post
from autopost.writer import finalize
from fakes import post_json


def test_menu_matches_requested_order():
    links = menu_links("https://aibaekgwa.blogspot.com")
    assert [name for name, _ in links] == [
        "홈", "AI 언어 모델", "AI 도구", "AI 논문 분석", "AI 뉴스 리뷰", "AI 보안", "AI 인사이트", "AI 공모주 분석",
    ]
    assert links[0][1] == "https://aibaekgwa.blogspot.com/"
    assert links[1][1] == "https://aibaekgwa.blogspot.com/search/label/AI%20%EC%96%B8%EC%96%B4%20%EB%AA%A8%EB%8D%B8"


def test_every_post_is_labelled_with_its_menu_category():
    for category in CATEGORIES:
        labels = build_labels(category, ["#Gemini"])
        assert labels[0] == category
        assert label_url("https://x.blogspot.com/", labels[0]).endswith("/search/label/" + label_url("", category).split("/")[-1])


def test_ipo_posts_always_carry_disclaimer():
    ipo = finalize(parse_post(post_json(category="AI 공모주 분석", caveat="일정은 바뀔 수 있습니다.")))
    assert ipo.caveat == f"일정은 바뀔 수 있습니다. {IPO_DISCLAIMER}"
    assert finalize(ipo).caveat.count(IPO_DISCLAIMER) == 1
    other = finalize(parse_post(post_json()))
    assert IPO_DISCLAIMER not in other.caveat
