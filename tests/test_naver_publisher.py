import base64
import json
from pathlib import Path

import pytest

from autopost.content import Block, Reference, parse_post
from autopost.naver_publisher import (
    NaverError,
    NaverExtras,
    build_blocks,
    convert_block,
    decode_storage_state,
    encode_storage_state,
    extract_post_url,
)
from fakes import post_json


def test_blocks_follow_same_order_as_google():
    content = parse_post(post_json()).with_references([Reference("OpenAI", "2026", "T", "https://openai.com/a")])
    blocks = build_blocks(content, NaverExtras(Path("cover.png"), (("예전 글", "https://blog.naver.com/me/1"),), "기준일"))
    kinds = [b.kind for b in blocks]
    assert kinds[:3] == ["text", "image", "text"]
    headings = [b.text for b in blocks if b.kind == "heading"]
    assert headings == ["📰 Gemini 3.8 Live 등장 배경", "⚙️ 공식 스펙 정리", "❓ 자주 묻는 질문",
                        "🔗 함께 보면 좋은 글", "🎯 맺음말", "📚 참고 문헌 및 출처"]
    assert blocks[2].text.startswith("**📌 한눈에 보기**\n- Gemini 3.8 Live 공개")
    assert "**Q1. 무료인가요?**" in [b.text.split("\n")[0] for b in blocks]
    assert blocks[-2] .kind == "code" and "Retrieved from https://openai.com/a" in blocks[-2].text
    assert blocks[-1].text == "기준일"


def test_minimal_post_has_no_optional_blocks():
    blocks = build_blocks(parse_post(post_json(summary=[], faq=[])))
    assert all(b.kind != "image" for b in blocks)
    assert [b.text for b in blocks if b.kind == "heading"][-1] == "🎯 맺음말"


def test_block_conversion_for_editor_without_html():
    assert convert_block(Block("info", text="내용", title="팁")).text == "**💡 팁** — 내용"
    assert convert_block(Block("warning", text="내용")).text == "⚠️ 내용"
    two = convert_block(Block("table", headers=("항목", "내용"), rows=(("가격", "무료"),)))
    assert two.text == "• **가격**: 무료"
    three = convert_block(Block("table", headers=("모델", "입력", "출력"), rows=(("A", "$1", ""), ("B", "", ""))))
    assert three.text == "• **A** — 입력: $1\n• **B**"
    assert convert_block(Block("code", text="  x = 1")).kind == "code"
    assert convert_block(Block("list", items=("a", "b"))).text == "• a\n• b"
    assert convert_block(Block("subheading", text="h3")).kind == "subheading"
    assert convert_block(Block("paragraph", text="a\n\nb")).text == "a\nb"


def test_storage_state_roundtrip_keeps_only_naver():
    state = {"cookies": [{"name": "NID_AUT", "domain": ".naver.com"}, {"name": "x", "domain": "evil.com"}],
             "origins": [{"origin": "https://blog.naver.com"}, {"origin": "https://other.com"}]}
    decoded = decode_storage_state(encode_storage_state(state))
    assert [c["name"] for c in decoded["cookies"]] == ["NID_AUT"]
    assert [o["origin"] for o in decoded["origins"]] == ["https://blog.naver.com"]
    with pytest.raises(NaverError):
        decode_storage_state("not-base64!")


def test_large_state_drops_origin_storage():
    state = {"cookies": [{"name": "NID_AUT", "domain": ".naver.com"}],
             "origins": [{"origin": "https://blog.naver.com", "localStorage": [{"name": "x", "value": "a" * 60000}]}]}
    decoded = json.loads(base64.b64decode(encode_storage_state(state)))
    assert decoded["origins"] == []


def test_extract_post_url():
    assert extract_post_url("me", ["https://blog.naver.com/PostView.naver?blogId=me&logNo=223456789012"]) == \
        "https://blog.naver.com/me/223456789012"
    assert extract_post_url("me", ["https://blog.naver.com/other/223456789012", "about:blank"]) == ""
