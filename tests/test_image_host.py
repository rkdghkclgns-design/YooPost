import pytest

from autopost.image_host import ImageHost, ImageHostError, safe_key


class Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.puts = []

    def _next(self):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def put(self, url, data, headers, timeout):
        self.puts.append((url, data, headers))
        return self._next()

    def get(self, url, timeout):
        return self._next()


def host(responses):
    session = Session(responses)
    return ImageHost("https://relay.example/", "tok", session, sleep=lambda s: None), session


def test_upload_sends_token_and_returns_public_url(tmp_path):
    image = tmp_path / "cover.png"
    image.write_bytes(b"png")
    uploader, session = host([Response(201, {"ok": True, "url": "https://relay.example/img/a_b.png"})])
    assert uploader.upload(image, "a b.png") == "https://relay.example/img/a_b.png"
    url, data, headers = session.puts[0]
    assert url == "https://relay.example/img/a_b.png" and data == b"png"
    assert headers == {"Authorization": "Bearer tok", "Content-Type": "image/png"}


def test_upload_retries_server_errors_but_not_client_errors(tmp_path):
    image = tmp_path / "q.jpg"
    image.write_bytes(b"jpg")
    uploader, session = host([ConnectionError("reset"), Response(502), Response(201, {})])
    assert uploader.upload(image, "q.jpg") == "https://relay.example/img/q.jpg"
    assert len(session.puts) == 3
    uploader, session = host([Response(401)])
    with pytest.raises(ImageHostError, match="HTTP 401"):
        uploader.upload(image, "q.jpg")
    assert len(session.puts) == 1
    uploader, _ = host([ConnectionError("x")] * 3)
    with pytest.raises(ImageHostError, match="ConnectionError"):
        uploader.upload(image, "q.jpg")


def test_rejects_unknown_types_and_empty_names(tmp_path):
    svg = tmp_path / "a.svg"
    svg.write_text("<svg/>")
    uploader, _ = host([])
    with pytest.raises(ImageHostError, match="형식"):
        uploader.upload(svg, "a.svg")
    with pytest.raises(ImageHostError, match="비었습니다"):
        safe_key("///")
    assert safe_key("../20260929 cover.png") == "20260929_cover.png"


def test_ping():
    assert host([Response(200)])[0].ping()
    assert not host([Response(503)])[0].ping()
    assert not host([ConnectionError("down")])[0].ping()
