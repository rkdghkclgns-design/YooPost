from autopost.links import canonical, check_link, domain, is_http_url, resolve_grounding_url, unique_urls
from fakes import FakeHttp, FakeHttpResponse

GROUNDING = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"


def test_resolve_grounding_redirect_to_original_url():
    http = FakeHttp({GROUNDING: FakeHttpResponse(302, headers={"Location": "https://openai.com/news/x"})})
    assert resolve_grounding_url(GROUNDING, http) == "https://openai.com/news/x"
    assert http.calls[0][1]["allow_redirects"] is False
    assert resolve_grounding_url("https://plain.com/a", http) == "https://plain.com/a"


def test_resolve_keeps_original_on_failure():
    assert resolve_grounding_url(GROUNDING, FakeHttp({GROUNDING: RuntimeError("down")})) == GROUNDING
    assert resolve_grounding_url(GROUNDING, FakeHttp({GROUNDING: FakeHttpResponse(200)})) == GROUNDING


def test_check_link_reports_status_title_and_mentions():
    page = "<html><title> Gemini 3.8 \n Live </title><body>Gemini 3.8 is here</body></html>"
    http = FakeHttp({"https://a.com/x": FakeHttpResponse(200, page)})
    check = check_link("https://a.com/x", ["gemini 3.8"], http)
    assert check.alive and check.mentions and check.title == "Gemini 3.8 Live"
    assert not check_link("https://a.com/x", ["Claude"], http).mentions


def test_check_link_failure_modes():
    http = FakeHttp({"https://b.com": FakeHttpResponse(403), "https://c.com": RuntimeError("dns")})
    blocked = check_link("https://b.com", ["x"], http)
    assert blocked.blocked and not blocked.alive
    assert check_link("https://c.com", ["x"], http, wait=0).status == 0
    assert check_link("https://missing.com", ["x"], http).status == 404
    assert check_link("ftp://nope", ["x"], http).status == 0


def test_korean_page_without_charset_is_decoded_by_content():
    class Response:
        status_code, url, encoding, apparent_encoding = 200, "https://kr.com/a", "ISO-8859-1", "euc-kr"
        headers = {"Content-Type": "text/html"}
        content = "<title>제미나이 공개</title>제미나이 3.8".encode("euc-kr")

    class Session:
        def get(self, url, **kwargs):
            return Response()

    check = check_link("https://kr.com/a", ["제미나이"], Session())
    assert check.mentions and check.title == "제미나이 공개"


def test_connection_error_is_retried_once():
    class Flaky:
        calls = 0

        def get(self, url, **kwargs):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise ConnectionError("reset")
            return FakeHttpResponse(200, "Gemini", url=url)

    assert check_link("https://a.com", ["gemini"], Flaky(), wait=0).alive and Flaky.calls == 2
    always_down = FakeHttp({"https://down.com": ConnectionError("down")})
    assert check_link("https://down.com", ["x"], always_down, wait=0).status == 0
    assert len(always_down.calls) == 2


def test_non_html_page_has_no_text():
    http = FakeHttp({"https://p.com/a.pdf": FakeHttpResponse(200, "Gemini", {"Content-Type": "application/pdf"})})
    check = check_link("https://p.com/a.pdf", ["gemini"], http)
    assert check.alive and not check.mentions


def test_url_helpers():
    assert is_http_url("https://x.com") and not is_http_url("javascript:alert(1)")
    assert domain("https://www.OpenAI.com/a") == "openai.com"
    assert canonical("https://www.a.com/p/?utm_source=x&id=1") == canonical("http://a.com/p?id=1")
    assert unique_urls(["https://a.com/p", "https://www.a.com/p/", "", "https://b.com"]) == ["https://a.com/p", "https://b.com"]
