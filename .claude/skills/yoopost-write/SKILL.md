---
name: yoopost-write
description: AI백과사전(구글 블로그) 원고를 Claude 가 직접 조사·작성·사실 검증해 슬랙 승인 요청까지 보낸다. "글 써줘", "포스팅해줘", "카테고리별로 7편", "AI 보안 글" 같은 요청에 사용. 구글 AI(Gemini) API 를 쓰지 않는다.
---

# AI백과사전 원고 작성 (Claude → 슬랙 ✅ → 발행)

원고는 Claude 가 쓴다. 구글 AI(Gemini) API 는 쓰지 않는다 (비용 0원). 발행은 사용자가 슬랙 원고 메시지에 ✅ 를 눌러야만 된다.
**Claude 는 절대 ✅ 를 누르거나 직접 발행하지 않는다** — 승인은 사용자의 몫이다.

## 역할 분담 (2026-09-29 사용자 결정)
1편마다 하위 에이전트 둘을 **차례로** 쓴다 (Agent 도구의 `model` 지정):
| 단계 | 모델 | 하는 일 |
|---|---|---|
| ① 작성 | **Sonnet** (`model: "sonnet"`) | 지침 확인·주제 선정·조사·원고(post.json)·출처(research.json) |
| ② 사실 검증 | **Opus** (`model: "opus"`) | 주장 전부 추출 → 출처를 다시 열어 확인 → 틀린 것 수정·확인 안 되는 문장 삭제 → factcheck 기록 → 슬랙 승인 요청 |
작성자는 슬랙에 보내지 않는다. 검증자만 `claude_draft.py` 로 보낸다. 검증자가 원고의 품질 기준(아래)을 확인하고 부족하면 보완한다.

## 카테고리 (블로그 메뉴 순서)
AI 언어 모델 · AI 도구 · AI 논문 분석 · AI 뉴스 리뷰 · AI 보안 · AI 인사이트 · AI 공모주 분석
"카테고리별로" / "전체" 요청이면 이 순서로 1편씩, 앞에서 고른 주제와 겹치지 않게 쓴다.
같은 날 이미 슬랙에 보낸 원고(원고 폴더에 publish.html 있음)가 있는 카테고리는 건너뛴다.

## ① 작성 단계 (Sonnet)
1. **지침 보기 + 원고 폴더 만들기**
   ```bash
   python scripts/claude_brief.py --category "AI 보안" --new
   ```
   출력의 글 문법·사실 규칙·post.json/research.json 형식·**이미 쓴 글 목록**·**최근 7일 원고(승인 대기 포함)**를 그대로 따른다. 마지막 줄이 원고 폴더 경로.
2. **주제 고르기**: WebSearch 로 최근(보통 3일, 논문·보안·공모주는 14일) 공식 발표·1차 자료가 있는 AI 소식. 이미 쓴 글·최근 원고·이번 실행의 앞 주제와 같은 주제·같은 원문 금지.
   - 공모주: 증권신고서·공시·공식 보도자료만 (최근 60일). 없으면 "건너뜀"으로 보고. 수익 전망·매수 권유 금지 (안내문은 시스템이 붙인다).
3. **조사**: WebFetch 로 공식 발표·문서·논문 원문을 직접 읽고, 신뢰할 수 있는 언론으로 교차 확인. 출처 4~10개, 출처 url 은 실제로 읽은 원래 주소.
   인용 이미지가 설명을 도우면 이미지가 실린 **출처 페이지**를 최대 3개 images 에 (파일 주소를 모르면 image_url 은 빈 문자열).
4. **원고 (post.json)**: 지침의 OUTPUT_SCHEMA 그대로. 참고 블로그 문법(이모지 제목, "안녕하세요!" 도입, 한눈에 보기, 이모지 h2, 💡/⚠️ 박스, 표, FAQ, 맺음말 끝인사),
   문단마다 핵심 1~3곳 `**형광 강조**`, 관련 문단 뒤 `{"type": "image", "image_id": "I1"}`. 본문 공백 제외 지침의 최소~목표 분량.
   발표일은 출처에 적힌 날짜만 (오늘 날짜를 발표일처럼 쓰지 않는다). `category` 는 요청 카테고리 그대로.
5. research.json 은 `topic`·`source_url`·`sources`·`images` 를 채우고 `factcheck` 는 비워 둔다 (검증자가 채움). 둘 다 UTF-8 로 저장.
6. 보고: 원고 폴더, 제목, 주제, 원문 주소, 본문 글자 수, 출처 수. **슬랙에 보내지 않는다.**

## ② 사실 검증 단계 (Opus)
1. 원고 폴더의 post.json·research.json 을 읽는다.
2. 원고의 수치·날짜·이름·가격·스펙·출시 여부 주장을 **모두** 뽑고, research.json 의 출처(필요하면 추가 검색)를 **다시 열어** 하나씩 확인한다.
3. 틀린 것은 고치고, 확인되지 않는 문장은 뺀다 (같은 내용이 도입·요약·표·FAQ 에 반복되면 모두). 확인에 쓴 새 출처는 sources 에 추가하고 source_ids 도 맞춘다.
4. **품질 기준 확인**: 본문 분량(지침 최소 이상), 이모지 h2 소제목 5개 이상, FAQ 4개 안팎, 표 2개 이상, 💡/⚠️ 박스, 문단마다 형광 강조, 맺음말 끝인사, 공식 출처 1개 이상.
   부족하면 확인된 사실로만 보완한다 (새 주장을 넣으면 그것도 검증).
5. research.json 의 `factcheck` 에 `{"claims": 전체 주장 수, "confirmed": 확인 수, "removed": [뺀 문장], "note": "검증 방법 (Opus 검증)"}` 기록.
6. **준비·슬랙 승인 요청**
   ```bash
   python scripts/claude_draft.py output/claude_drafts/<ID> --no-send   # 먼저 확인 (오류가 있으면 고침)
   python scripts/claude_draft.py output/claude_drafts/<ID>             # 슬랙 #yoopost-1-원고승인 에 '✅ 누르면 발행' 메시지
   ```
   출처가 열리지 않거나(404), 인용 이미지가 확인을 통과하지 못하면 자동으로 빼고 '참고'에 표시된다.
   "이미 발행한 주제와 겹칩니다" 가 나오면 멈추고 보고한다 (주제 교체는 작성 단계부터 다시).
7. `python scripts/draft_progress.py` 로 #yoopost-알림 진행 막대를 갱신한다.
8. 보고: 제목·카테고리·사실 검증 수치·보완한 내용·경고·슬랙 전송 여부. **✅ 는 사용자가 누르도록** 둔다.

## ☁️ 클라우드 루틴에서 쓸 때 (매일 07:00, PC 가 꺼져 있어도)
클라우드 세션에는 `.env`(비밀값)가 없고 슬랙·구글 블로그에 접속할 수 없다. 대신 원고를 **yoopost-drafts 브랜치**에 올리면
GitHub Actions(`cloud_drafts.yml`)가 위 ② 의 6~7 단계(준비·슬랙 승인 요청·진행 막대)를 대신한다. 아래만 바뀌고 나머지는 같다.
| 로컬 | 클라우드 |
|---|---|
| `python scripts/claude_brief.py --category X --new` | `python scripts/claude_brief.py --category X --new --drafts-dir .yoopost-drafts/drafts --published .yoopost-drafts/state/published.json` |
| `python scripts/claude_draft.py <폴더> --no-send` | `python scripts/cloud_drafts.py check <폴더>` (형식·분량·구성·factcheck 확인, 문제 있으면 고침) |
| `python scripts/claude_draft.py <폴더>` (슬랙 전송) | `python scripts/cloud_drafts.py push <폴더>` (올리면 Actions 가 슬랙 승인 요청) |
| `python scripts/draft_progress.py [--skip X --reason R]` | `python scripts/cloud_drafts.py skip --category X --reason R` (진행 막대는 Actions 가 갱신) |
실행 처음에 `python scripts/cloud_drafts.py setup` → `status`(이미 끝난 카테고리 확인) → `start`, 마지막에 `finish`(요약은 Actions 가 #yoopost-알림 에).
WebFetch 가 `EGRESS_BLOCKED` 로 막힌 사이트는 읽지 못한 것이다 — 출처로 쓰지 않는다 (읽은 페이지만 출처). 막힌 곳이 많으면 `start --network limited`.
출처 접속 확인·인용 이미지 내려받기·중복 주제 확인은 Actions 가 다시 한다 (실패하면 #yoopost-알림 에 이유가 온다).

## 발행 이후 (자동)
슬랙 ✅ → Cloudflare Worker(`/slack/events`) → GitHub Actions `publish_draft.yml` → 구글 블로그 발행 → 원고 메시지 스레드에 결과.
실패하면 스레드에 '다시 누르면 재시도' 가 남고, ✅ 를 다시 누르면 다시 시도한다. 이미 발행한 원고는 두 번 올라가지 않는다.
