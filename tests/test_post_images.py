from autopost.post_images import insert_figures, target_sections

BODY = ("<p>도입</p><div>표지</div>"
        "<h2 a>📰 하나</h2><div>박스</div><p>첫 문단</p><p>둘째</p>"
        "<h2 a>⚙️ 둘</h2><p>A</p>"
        "<h2 a>💰 셋</h2><p>B</p>"
        "<h2 a>🔍 넷</h2><p>C</p>"
        "<h2 a>❓ 자주 묻는 질문</h2><p>Q</p>")


def test_target_sections():
    assert target_sections(4, 2) == [0, 2] and target_sections(4, 1) == [0]
    assert target_sections(1, 2) == [0] and target_sections(0, 2) == [] and target_sections(3, 0) == []


def test_insert_figures_after_first_paragraph_of_first_and_middle_sections():
    out = insert_figures(BODY, ["<figure>1</figure>", "<figure>2</figure>"], 4)
    assert "<p>첫 문단</p><figure>1</figure><p>둘째</p>" in out
    assert "<h2 a>💰 셋</h2><p>B</p><figure>2</figure>" in out
    assert out.count("<figure") == 2
    assert insert_figures(out, ["<figure>3</figure>"], 4) == out  # 이미 그림이 있으면 그대로
    assert insert_figures(BODY, [], 4) == BODY
    no_paragraph = "<h2>하나</h2><ul><li>x</li></ul><h2>둘</h2>"
    assert insert_figures(no_paragraph, ["<figure>1</figure>"], 2) == "<h2>하나</h2><figure>1</figure><ul><li>x</li></ul><h2>둘</h2>"


def test_remove_auto_figures_drops_only_the_earlier_og_card_figures():
    from autopost.post_images import remove_auto_figures

    og = ('<figure style="x"><img src="https://img.example/f/20260929_1_auto_A1.jpg" alt="GPT-6"/>'
          '<figcaption>▲ GPT-6 (출처: <a href="https://openai.com">OpenAI</a>)</figcaption></figure>')
    kept = '<figure style="x"><img src="https://img.example/f/20260929_1_I1.png" alt="구조도"/></figure>'
    html = f"<h2>하나</h2><p>A</p>{og}<p>B</p>{kept}<h2>둘</h2><p>C</p>{og}"
    cleaned, removed = remove_auto_figures(html)
    assert removed == 2 and cleaned == f"<h2>하나</h2><p>A</p><p>B</p>{kept}<h2>둘</h2><p>C</p>"
    assert remove_auto_figures(BODY) == (BODY, 0)


def test_insert_figures_at_explicit_sections():
    out = insert_figures(BODY, ["<figure>1</figure>", "<figure>2</figure>"], 4, places=[3, 1])
    assert "<h2 a>🔍 넷</h2><p>C</p><figure>1</figure>" in out
    assert "<h2 a>⚙️ 둘</h2><p>A</p><figure>2</figure>" in out
    assert insert_figures(BODY, ["<figure>1</figure>"], 4, places=[9]) == BODY  # 없는 소제목은 건너뜀
