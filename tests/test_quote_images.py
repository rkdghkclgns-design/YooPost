import io

from PIL import Image

from autopost.quote_images import ImageCollector, og_image, parse_candidates, site
from autopost.research import ImageCandidate, Source
from fakes import FakeHttp, FakeHttpResponse

PAGE = "https://blogs.nvidia.com/blog/vera-cpu/"
SOURCES = (Source("S1", "Vera", PAGE, "NVIDIA", official=True),)


def png(size=(1200, 675), mode="RGB", color="white") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, "PNG")
    return buffer.getvalue()


def image_response(data: bytes, content_type="image/png") -> FakeHttpResponse:
    return FakeHttpResponse(200, headers={"Content-Type": content_type}, body=data)


def candidate(id="I1", image="https://blogs.nvidia.com/i/vera.png", page=PAGE, credit=""):
    return ImageCandidate(id, image, page, "Vera CPU 구조도", credit)


def test_parse_candidates_filters_and_limits():
    raw = [
        {"id": "i1", "image_url": "https://a.com/1.png", "page_url": "https://a.com/p", "caption": "  설명  1 "},
        {"image_url": "not-url", "page_url": "https://a.com/p", "caption": "페이지만"},
        {"image_url": "https://a.com/3.png", "caption": ""},  # 설명 없음
        "garbage", {"image_url": "bad", "page_url": "bad", "caption": "x"},
        {"url": "https://a.com/4.png", "caption": "4"}, {"url": "https://a.com/5.png", "caption": "5"},
        {"url": "https://a.com/6.png", "caption": "6"},
    ]
    found = parse_candidates(raw)
    assert [c.id for c in found] == ["I1", "I2", "I6"] and len(found) == 3
    assert found[0].caption == "설명 1" and found[1].image_url == "" and found[1].page_url == "https://a.com/p"
    assert parse_candidates(None) == ()


def test_og_image_reads_either_attribute_order_and_relative_urls():
    assert og_image('<meta property="og:image" content="/img/a.png">', "https://x.com/p/") == "https://x.com/img/a.png"
    assert og_image('<meta content="https://c.com/b.jpg?a=1&amp;b=2" name="og:image">', "https://x.com") == \
        "https://c.com/b.jpg?a=1&b=2"
    assert og_image("<html></html>", "https://x.com") == ""


ARTICLE = """<html><head>
<meta property="og:image" content="https://blogs.nvidia.com/og.jpg">
<meta name="twitter:image" content="https://blogs.nvidia.com/card.jpg"></head><body>
<header><img src="/i/header-hero.png" alt="Vera CPU architecture block diagram"></header>
<nav><img src="/i/menu.png" alt="Vera CPU"></nav>
<article><h2>Vera CPU architecture</h2>
<p>text</p>
<figure><img src="/i/arch.png" alt=""><figcaption>Vera CPU core block diagram</figcaption></figure>
<img src="/i/og.jpg" alt="Vera CPU architecture diagram">
<img src="/i/logo-nvidia.png" alt="Vera CPU architecture logo">
<img data-src="/i/bench.png" alt="Vera benchmark chart versus Grace CPU" width="1200">
<img src="/i/team.png" alt="NVIDIA team photo">
<img src="/i/tiny.png" alt="Vera CPU architecture" width="64">
</article>
<footer><img src="/i/foot.png" alt="Vera CPU block diagram"></footer></body></html>"""


def test_article_images_skip_chrome_meta_cards_and_thumbnails():
    from autopost.quote_images import article_images, meta_images

    found = article_images(ARTICLE, PAGE)
    urls = [i.url for i in found]
    # 머리글·메뉴·바닥글, og 주소, 로고, 작은 그림은 빠진다
    assert urls == ["https://blogs.nvidia.com/i/arch.png", "https://blogs.nvidia.com/i/bench.png",
                    "https://blogs.nvidia.com/i/team.png"]
    assert found[0].caption == "Vera CPU core block diagram" and found[0].heading == "Vera CPU architecture"
    assert meta_images(ARTICLE, PAGE) == {"https://blogs.nvidia.com/og.jpg", "https://blogs.nvidia.com/card.jpg"}


def test_relevance_ignores_brand_words_and_undescribed_images():
    from autopost.quote_images import PageImage, relevance, relevant_images, tokens

    words = tokens("Vera CPU 구조도 architecture benchmark NVIDIA")
    brand = tokens("NVIDIA nvidia")
    team = PageImage("u1", alt="NVIDIA team photo")
    bench = PageImage("u2", alt="Vera benchmark chart versus Grace CPU")
    blank = PageImage("u3", alt="", heading="Vera CPU architecture")
    captioned = PageImage("u4", caption="Vera block", heading="")
    assert relevance(team, words, brand) == 0 and relevance(blank, words, brand) == 0
    assert relevance(bench, words, brand) >= 2
    assert relevant_images([team, blank, captioned, bench], words, brand) == [bench, captioned]


def test_missing_image_falls_back_to_a_related_article_figure_never_the_og_card(tmp_path):
    http = FakeHttp({
        "https://blogs.nvidia.com/i/vera.png": image_response(png()),
        PAGE: FakeHttpResponse(200, ARTICLE),
        "https://blogs.nvidia.com/i/arch.png": image_response(png((1600, 900), color="black"), "image/jpeg"),
        "https://blogs.nvidia.com/og.jpg": image_response(png((1600, 900), color="gray"), "image/jpeg"),
    })
    found = ImageCollector(http).collect(
        [candidate(), candidate("I2", image="https://blogs.nvidia.com/i/missing.png", credit="NVIDIA Newsroom")],
        SOURCES, tmp_path, keywords={"vera", "cpu", "구조도"})
    assert [i.id for i in found] == ["I1", "I2"]
    assert found[0].credit == "NVIDIA" and found[0].path.suffix == ".jpg" and found[0].width == 1200
    assert found[1].credit == "NVIDIA Newsroom" and found[1].width == 1400  # 본문 폭에 맞게 줄임
    assert not any(url == "https://blogs.nvidia.com/og.jpg" for url, _ in http.calls)  # 대표 이미지는 받지도 않는다


def test_writer_supplied_og_or_thumbnail_urls_are_refused(tmp_path):
    http = FakeHttp({
        PAGE: FakeHttpResponse(200, '<meta property="og:image" content="https://blogs.nvidia.com/og.jpg"><p>x</p>'),
        "https://blogs.nvidia.com/og.jpg": image_response(png()),
        "https://blogs.nvidia.com/thumbnails/vera.png": image_response(png()),
    })
    found = ImageCollector(http).collect(
        [candidate("I1", image="https://blogs.nvidia.com/og.jpg"),
         candidate("I2", image="https://blogs.nvidia.com/thumbnails/vera.png")], SOURCES, tmp_path, keywords={"vera"})
    assert found == ()


def test_rejects_other_sites_small_duplicate_svg_and_broken_images(tmp_path):
    same = png()
    http = FakeHttp({
        "https://blogs.nvidia.com/i/a.png": image_response(same),
        "https://blogs.nvidia.com/i/dup.png": image_response(same),
        "https://blogs.nvidia.com/i/icon.png": image_response(png((64, 64))),
        "https://blogs.nvidia.com/i/banner.png": image_response(png((2000, 300))),
        "https://blogs.nvidia.com/i/logo.svg": image_response(b"<svg/>", "image/svg+xml"),
        "https://blogs.nvidia.com/i/broken.png": image_response(b"not an image"),
        "https://blogs.nvidia.com/i/err.png": RuntimeError("timeout"),
    })
    candidates = [candidate("I1", "https://blogs.nvidia.com/i/a.png"),
                  candidate("I2", "https://blogs.nvidia.com/i/dup.png"),
                  candidate("I3", "https://blogs.nvidia.com/i/icon.png", page=""),
                  candidate("I4", "https://blogs.nvidia.com/i/banner.png", page=""),
                  candidate("I5", "https://blogs.nvidia.com/i/logo.svg", page=""),
                  candidate("I6", "https://blogs.nvidia.com/i/broken.png", page=""),
                  candidate("I7", "https://blogs.nvidia.com/i/err.png", page=""),
                  candidate("I8", "https://evil.example/x.png", page="https://evil.example/p")]
    found = ImageCollector(http).collect(candidates, SOURCES, tmp_path)
    assert [i.id for i in found] == ["I1"]
    assert not any("evil.example" in url for url, _ in http.calls)  # 확인된 출처가 아닌 사이트는 열지도 않는다


def test_transparent_images_stay_png_and_page_errors_are_skipped(tmp_path):
    http = FakeHttp({
        "https://blogs.nvidia.com/i/t.png": image_response(png(mode="RGBA", color=(0, 0, 0, 0))),
        PAGE: RuntimeError("reset"),
    })
    found = ImageCollector(http).collect(
        [candidate("I1", "https://blogs.nvidia.com/i/t.png"), candidate("I2", "")], SOURCES, tmp_path)
    assert [i.id for i in found] == ["I1"] and found[0].path.suffix == ".png"


def test_site_groups_subdomains_of_the_same_company():
    assert site("https://blogs.nvidia.com/x") == site("https://nvidianews.nvidia.com/y") == "nvidia.com"
    assert site("https://news.naver.co.kr/a") == "naver.co.kr" and site("https://www.aitimes.com/a") == "aitimes.com"
    assert site("https://openai.com") == "openai.com"
    # 공유 호스팅은 하위 주소가 달라지면 다른 사이트 (남의 github.io 이미지 차단)
    assert site("https://official.github.io/p") != site("https://attacker.github.io/x.png")
    assert site("https://a.b.github.io/x") == "a.b.github.io"


def test_oversized_downloads_are_dropped_without_reading_everything(tmp_path, monkeypatch):
    from autopost import quote_images

    monkeypatch.setattr(quote_images, "MAX_DOWNLOAD", 1000)
    big = image_response(png())
    declared = FakeHttpResponse(200, headers={"Content-Type": "image/png", "Content-Length": "999999"}, body=png())
    http = FakeHttp({"https://blogs.nvidia.com/i/big.png": big, "https://blogs.nvidia.com/i/declared.png": declared})
    found = ImageCollector(http).collect([candidate("I1", "https://blogs.nvidia.com/i/big.png", page=""),
                                          candidate("I2", "https://blogs.nvidia.com/i/declared.png", page="")],
                                         SOURCES, tmp_path)
    assert found == ()
    assert all(kwargs.get("stream") for _, kwargs in http.calls)
