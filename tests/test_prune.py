import json

from autopost.constants import CLOSING_SIGNATURE
from autopost.content import parse_post
from autopost.fact_check import ClaimVerdict
from autopost.prune import Pruner, heuristic_matches, remove_units, removed_markdown, sentences, units_of
from fakes import fake_gemini, post_dict


def post(**overrides):
    data = post_dict(**overrides)
    data["sections"][0]["blocks"] = [
        {"type": "paragraph", "text": "Vera 는 **88개** 코어를 씁니다. 발표일은 2026년 7월입니다.\n\n둘째 문단입니다."},
        {"type": "subheading", "text": "세부 스펙"},
        {"type": "info", "title": "팁", "text": "TDP 는 공개되지 않았습니다."},
    ]
    data["sections"][1]["blocks"] = [
        {"type": "table", "headers": ["항목", "값"], "rows": [["코어", "88개"], ["출시", "2026년 7월"]]},
        {"type": "list", "items": ["샌드박스 1.8배", "캐시 미공개"]},
        {"type": "code", "language": "bash", "code": "echo 2026년 7월"},
    ]
    data["faq"] = [{"q": "언제 나왔나요?", "a": "2026년 7월입니다."}, {"q": "코어 수는?", "a": "88개입니다."}]
    data["closing"] = "지금까지 알아보았습니다. 7월 출시가 핵심입니다."
    return parse_post(json.dumps(data, ensure_ascii=False))


def ids_of(content, *needles):
    units = units_of(content)
    return [u.id for u in units if any(n in u.text for n in needles)]


def test_units_cover_sentences_items_rows_and_faq_but_not_code():
    content = post()
    texts = [u.text for u in units_of(content)]
    assert "Vera 는 88개 코어를 씁니다." in texts  # ** 표시는 빼고 보여 준다
    assert "발표일은 2026년 7월입니다." in texts and "출시 | 2026년 7월" in texts
    assert "Q. 언제 나왔나요? A. 2026년 7월입니다." in texts and "캐시 미공개" in texts
    assert not any("echo" in t for t in texts) and not any(t == "세부 스펙" for t in texts)
    assert sentences("가. 나! 다? 라") == ["가.", "나!", "다?", "라"]


def test_remove_units_deletes_only_chosen_places_and_tidies_up():
    content = post()
    by_text = {u.text: u.path for u in units_of(content)}
    chosen = {by_text["발표일은 2026년 7월입니다."], by_text["출시 | 2026년 7월"], by_text["Q. 언제 나왔나요? A. 2026년 7월입니다."],
              by_text["TDP 는 공개되지 않았습니다."], by_text["7월 출시가 핵심입니다."]}
    pruned = remove_units(content, chosen)
    first = pruned.sections[0].blocks
    assert first[0].text == "Vera 는 **88개** 코어를 씁니다.\n\n둘째 문단입니다."
    assert [b.kind for b in first] == ["paragraph"]  # 빈 박스와 뒤따를 내용이 없는 소제목은 정리
    assert pruned.sections[1].blocks[0].rows == (("코어", "88개"),)
    assert pruned.sections[1].blocks[2].kind == "code"  # 코드블록은 건드리지 않음
    assert [f.question for f in pruned.faq] == ["코어 수는?"]
    assert pruned.closing == "지금까지 알아보았습니다."
    assert pruned.title == content.title and pruned.summary == content.summary


def test_empty_sections_are_dropped_and_closing_keeps_signature():
    content = post()
    everything = {u.path for u in units_of(content)}
    pruned = remove_units(content, everything)
    assert len(pruned.sections) == 1 and pruned.sections[0].blocks[0].kind == "code"
    assert pruned.closing == CLOSING_SIGNATURE and pruned.intro == "" and pruned.faq == ()


def test_pruner_uses_ai_matches_by_number_only():
    content = post()
    march = ids_of(content, "2026년 7월", "7월 출시")
    reply = json.dumps({"matches": [{"claim": "C1", "units": march + ["U999"]}, {"claim": "C2", "units": []},
                                    "garbage", {"claim": "C9", "units": ["U1"]}]})
    gemini, client = fake_gemini([reply])
    issues = [ClaimVerdict("Vera 는 2026년 7월에 출시되었다.", "contradicted", correction="5월"),
              ClaimVerdict("양자 컴퓨터를 내장했다.", "unverifiable")]
    result = Pruner(gemini).prune(content, issues)
    assert len(result.removed) == len(march) and all("7월" in t for t in result.removed)
    assert result.unlocated == ("양자 컴퓨터를 내장했다.",)
    assert "7월" not in result.content.plain_text().replace("echo 2026년 7월", "")
    prompt = client.models.calls[0]["contents"]
    assert "C1: Vera 는 2026년 7월에 출시되었다." in prompt and "U1: " in prompt


def test_pruner_falls_back_to_word_overlap_when_ai_fails():
    content = post()
    gemini, _ = fake_gemini([RuntimeError("503"), "not json"], models=("m1", "m2"))
    issues = [ClaimVerdict("TDP 는 공개되지 않았습니다", "contradicted")]
    result = Pruner(gemini).prune(content, issues)
    assert result.removed == ("TDP 는 공개되지 않았습니다.",) and result.unlocated == ()


def test_heuristic_requires_matching_numbers():
    units = units_of(post())
    assert heuristic_matches(units, "코어는 96개입니다") == []  # 숫자가 다르면 같은 문장으로 보지 않음
    assert heuristic_matches(units, "") == []
    assert Pruner(fake_gemini([])[0]).prune(post(), []).removed == ()


def test_removed_markdown_lists_removed_and_unlocated():
    text = removed_markdown(["문장 A"], ["주장 B"])
    assert "## 발행 전 제외한 문장·항목 (1곳)" in text and "- 문장 A" in text and "- 주장 B" in text
    assert "위치를 찾지 못한" not in removed_markdown(["문장 A"])
