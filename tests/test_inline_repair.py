from autopost.inline_repair import repair_html_links
from autopost.publish_draft import publication_html


def test_repairs_multiple_citations_preserving_markup_and_marker():
    source = ('<p class="body"><b style="color:blue">공개일</b> '
              '[공개 이력](https://example.com/a_(b)?a=1&amp;b=2) · '
              '[메모리 이력](https://example.com/m)</p>\n'
              '<!--yoopost {"draft_id":"d1","topic":"[문법](https://example.com)"}-->')
    result = repair_html_links(source)
    assert result.changes == 2
    assert '<b style="color:blue">공개일</b>' in result.html
    assert 'href="https://example.com/a_(b)?a=1&amp;b=2"' in result.html
    assert '>공개 이력</a>' in result.html
    assert source[source.index('<!--yoopost'):] in result.html
    assert repair_html_links(result.html).changes == 0
    assert repair_html_links(result.html).html == result.html


def test_does_not_touch_code_existing_links_attributes_or_scripts():
    source = ('<p title="[속성](https://example.com)">[출처](https://example.com)</p>'
              '<pre><code>[코드](https://example.com)</code></pre>'
              '<a href="https://example.com">[기존 링크](https://example.com)</a>'
              '<script>const x="[스크립트](https://example.com)";</script>'
              '<style>/* [스타일](https://example.com) */</style>'
              '<textarea>[입력](https://example.com)</textarea>')
    result = repair_html_links(source)
    assert result.changes == 1
    assert source[source.index('<pre>'):] in result.html
    assert 'title="[속성](https://example.com)"' in result.html


def test_escaped_label_remains_text_and_plain_urls_are_unchanged():
    source = '<p>[&lt;img src=x onerror=alert(1)&gt;](https://example.com) https://example.com</p>'
    result = repair_html_links(source)
    assert result.changes == 1 and '<img ' not in result.html
    assert '&lt;img src=x onerror=alert(1)&gt;</a>' in result.html
    assert result.html.endswith(' https://example.com</p>')


def test_publishing_repairs_already_prepared_draft_without_changing_draft_id():
    rendered = publication_html('d1', {'html':'<p>[공식](https://example.com)</p>', 'category':'AI 보안'})
    assert '>공식</a>' in rendered and '[공식](' not in rendered
    assert '"draft_id":"d1"' in rendered
