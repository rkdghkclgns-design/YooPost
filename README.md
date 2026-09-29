# AI백과사전 자동 포스팅 (YooPost)

매일 **AI 공식 발표·뉴스**에서 주제를 골라, **구글 검색(무료)**으로 조사하고 **사실을 교차 확인한 정보만** 담아
**구글 블로그 [AI백과사전](https://aibaekgwasajeon.blogspot.com/)**에 올리고, 결과를 **슬랙 YooPost 워크스페이스**로 보고합니다.
글의 구성·서식은 참고 블로그 [fornewchallenge.tistory.com](https://fornewchallenge.tistory.com/) **전체 게시물을 분석한 문법**을 그대로 따릅니다.
영상은 만들지 않고, 네이버 블로그와 **구글 드라이브는 쓰지 않습니다**. GitHub Actions 에서 동작하며, 구조는 `본연 포스팅 자동화`와 같습니다.

```
 [슬랙 /aipost] ─▶ Cloudflare Worker ─▶ GitHub Actions ◀─ [매일 11:00]
                    (서명 검증·중계        │
                     + 이미지 저장소)      ▼
 ① 주제 선정 ─▶ ② 구글 검색 조사·출처 확인·인용 이미지 ─▶ ③ AI 원고 (참고 블로그 문법·형광 강조)
                                                                  │
   슬랙 결과 카드 ◀─ ⑥ 구글 블로그 발행 ◀─ ⑤ 대표 이미지·미리보기 ◀─ ④ 사실 검증 (틀리면 고쳐서 재검증)
                     (글 목록 = 발행 기록)                          └ 그래도 남은 문제 문장·항목만 빼고 발행
```

---

## '사실이 확인된 정보'를 보장하는 방법

| 단계 | 하는 일 | 걸러내는 것 |
|---|---|---|
| ① 주제 선정 | OpenAI·Google DeepMind·Google AI·Hugging Face·Mistral·NVIDIA·AWS·GitHub **공식 발표 RSS**를 먼저, AI타임스·GeekNews·TechCrunch·The Verge·HF 논문·해커뉴스를 보조로 수집 → 아직 안 쓴 주제 중 AI가 1개 선택 | 이미 쓴 주제(발행 기록), AI와 무관한 글 |
| ② 자료 조사 | Gemini + **구글 검색 근거(grounding, 무료)** 로 사실 15~30개·출처·인용 이미지가 실린 페이지를 모음. 사실마다 출처 번호 | 출처 없는 사실 |
| ② 출처 확인 | 출처 주소를 **직접 열어** 확인: 404·없는 도메인 제외, 페이지에 주제어가 없으면 제외, 봇 차단(403)은 구글 검색 근거로 실제 확인된 주소만 인정 | **AI가 지어낸 주소**, 엉뚱한 페이지 |
| ② 인용 이미지 | 조사에서 고른 출처 페이지의 공식 이미지(제품 사진·구조도·도표)를 **실제로 내려받아** 확인: 확인된 출처와 같은 사이트, 아이콘·로고 크기 제외, 안 되면 그 페이지 대표 이미지. 필요 없는 주제면 넣지 않음 | 지어낸 이미지 주소, 엉뚱한 사이트 이미지 |
| ③ 원고 | 확인된 사실 목록만 쓰도록 지시 (수치·날짜 그대로, 모르면 '공개되지 않았습니다') | 사실 목록 밖의 수치·기능·후기 |
| ④ 사실 검증 | 원고에서 수치·날짜·이름·가격·스펙 주장을 모두 뽑아 **원고와 별개로 다시 웹 검색**해 확인 / 틀림 / 확인 불가 판정 | 틀린 수치, 근거 없는 주장 |
| ④ 수정 | 틀림·확인 불가 주장을 고치거나 지운 뒤 **처음부터 다시 검증** (기본 2회) | |
| ④ 제외 | 그래도 남은 틀림·확인 불가 주장이 든 **문장·표의 행·목록 항목·FAQ 만 빼고 발행** (AI 는 번호만 고르고 삭제는 코드가 함 → 새 내용이 끼어들지 않음). 뺀 목록은 `factcheck.md` 끝에 | 확인 안 된 문장의 발행 |
| ④ 보류 | 뺄 문장을 원고에서 찾지 못했거나, 빼고 나니 본문이 60% 미만만 남으면 그때만 **발행 보류** | 믿기 어려운 글의 발행 |
| 참고 문헌 | 확인된 출처만 코드가 APA 형식으로 붙임 (AI가 참고문헌 주소를 쓸 수 없음) | 가짜 참고문헌 |

슬랙 원고 채널에 올라가는 `factcheck.md` 에서 주장마다 ✅ 확인 / ❌ 틀림 / ❔ 확인 불가 와 근거 주소를 볼 수 있습니다.

## 참고 블로그 문법 (전체 게시물 분석)

`scripts/analyze_reference_blog.py` 가 사이트맵의 **모든 게시물**을 1초 간격으로 확인해(robots.txt 준수, 본문은 저장하지 않음)
구성 통계만 [`autopost/data/style_profile.json`](autopost/data/style_profile.json) 에 저장하고, 원고 프롬프트가 이 프로필을 따릅니다.

**분석 결과 (2026-09-29, 게시물 950개 전체 · 2023-09 ~ 2026-09, 실패 0)** — 원고는 최근 120개 기준 '현재 문법'을 따릅니다.

| 항목 | 최근 120개 | 전체 950개 |
|---|---|---|
| 본문 분량 (공백 제외, 중앙값) | 7,914자 | 6,241자 |
| h2 소제목 / h3 | 10 / 10 | 7 / 6 |
| 표 / 코드블록 / 💡정보 박스 / ⚠️주의 박스 | 4 / 4 / 2 / 1.5 | 3 / 3 / 1 / 1 |
| '한눈에 보기' 요약 박스 | 80% | 12% |
| ❓ 자주 묻는 질문 (질문 5개 안팎) | 90% | 18% |
| 🔗 함께 보면 좋은 글 | 93% | 12% |
| 🎯 맺음말 / 📚 참고 문헌 및 출처 (출처 5개 안팎) | 100% / 99% | 84% / 46% |
| 제목 맨 앞 이모지 (🆓 🤖 ⚡ 🔒 🚀 순) | 97% | 70% |
| "안녕하세요!"로 시작 | 100% | 69% |
| "…다음 시간에 더 유익한 정보를 가지고 다시 찾아뵙겠습니다"로 끝 | 85% | 58% |

자주 쓰인 소제목: 📰 등장 배경과 의미 · ⚙ 공식 스펙 정리 · 📊 벤치마크 성능 · 💻 설치·사용 가이드 (에이전트에게 붙여넣기) ·
🧪 성능 테스트 예제 프롬프트 4가지 · 💡 활용 시나리오 3가지 · 🛠 실전 활용 · 💬 커뮤니티 반응 · ⚠ 한계점과 주의사항
카테고리: AI 도구 494 · AI 언어 모델 297 · AI 논문 분석 39 · AI 인사이트 32 · AI 뉴스 리뷰 24 · AI 공모주 분석 22 …

AI 원고는 위 문법을 그대로 따릅니다.

1. **제목**: 이모지 + 제품·모델명 + 핵심 수치·혜택 + '사용 가이드/총정리'
2. **도입**: "안녕하세요! {날짜}, {회사}가 {제품}을 공개했습니다 … 함께 살펴보시죠!" → 대표 이미지(1200×630)
3. **한눈에 보기** 박스 (파란 박스, 마지막 줄 "이 글에서 배울 것")
4. **이모지 소제목(h2)** 섹션: 📰 등장 배경 · ⚙️ 공식 스펙 정리(표) · 💰 가격 · 💻 설치·사용 가이드(에이전트에게 붙여넣기) · 🧪 예제 프롬프트 · 💡 활용 시나리오 · ⚠️ 제약 …
   — 💡 노란 정보 박스, ⚠️ 빨간 주의 박스, 파란 머리글 표, 검은 코드블록 (참고 블로그 CSS 색 그대로)
   — **중요 내용 강조**: 문단마다 핵심 수치·결론·제품명 1~3곳을 **굵은 글씨 + 연보라 형광 배경**으로 (참고 블로그와 같은 모양)
   — **인용 이미지**: 조사에서 확보한 공식 이미지를 관련 문단 아래에 `▲ 설명 (출처: 링크)` 캡션과 함께 (필요 없으면 생략)
5. **❓ 자주 묻는 질문** (Q1~Q6) → **🔗 함께 보면 좋은 글**(내 예전 글, 발행 기록에서) → **🎯 맺음말**
   ("지금까지 … 알아보았습니다 … 저는 다음 시간에 더 유익한 정보를 가지고 다시 찾아뵙겠습니다. 감사합니다.")
6. **📚 참고 문헌 및 출처** (APA) → "※ 본 글은 YYYY년 M월 D일 기준으로 작성되었습니다."

> 참고 블로그의 **글을 복사하지 않습니다.** 서식(문법)만 따르고, 내용은 공식 발표·1차 자료에서 새로 씁니다.

---

## 슬랙 (YooPost 워크스페이스)

실행마다 **진행 메시지 1개**가 올라오고 단계마다 갱신됩니다. 결과물은 **단계별 채널**로 갑니다.

| 채널 | 내용 | 설정 |
|---|---|---|
| #yoopost-알림 | 진행 체크리스트 · 오류 | `SLACK_CHANNEL_ID` (Secret) |
| #yoopost-1-주제조사 | 고른 주제 · 확인된 출처·사실 목록(sources.md) · 인용 이미지 | `SLACK_CHANNEL_RESEARCH` |
| #yoopost-2-원고검증 | 원고(post.txt) · 사실 검증 보고(factcheck.md) · 대표 이미지 · 구글 블로그 미리보기 | `SLACK_CHANNEL_DRAFT` |
| #yoopost-3-발행결과 | 발행 결과 링크 | `SLACK_CHANNEL_PUBLISH` |

```
🤖 AI 포스팅 진행 중 — Introducing Gemini 3.8 Live       → 끝나면 ✅ 포스팅 완료 — 🤖 Gemini 3.8 Live 사용 가이드…
정기 자동 실행 · 09/30 11:04
✅ 주제 선정 — 후보 48개 중 선택 (공식·Google DeepMind) · AI 언어 모델
✅ 자료 조사·출처 확인 — 사실 22개 · 출처 6개 (공식 3) · 인용 이미지 2개
✅ AI 원고 작성 — 본문 8,420자 · 소제목 9개
✅ 사실 검증 — 주장 31개 중 31개 확인 (검증 2회)
✅ 이미지 (대표·인용) — 대표 1200×630 · 인용 2개
✅ 블로그 발행 — https://aibaekgwasajeon.blogspot.com/2026/09/….html
```

| 슬랙 명령 | 동작 |
|---|---|
| `/aipost` | 최신 AI 소식에서 주제를 골라 포스팅 |
| `/aipost Gemini 3.8 Live` | 주제 지정 (제품·모델·논문 이름) — 사실로 확인할 수 없는 주제는 거절 |
| `/aipost 테스트` | 발행 없이 조사·원고·사실 검증·미리보기만 (`/aipost GPT-6 테스트` 가능) |
| `/aipost 도움말` | 사용법 안내 |
| `/aipost-setup` | 구글 블로그·Gemini·이미지 저장소 연동 상태 점검 |

슬랙 명령은 Cloudflare Worker(`https://yoopost-slack-relay.cleaning-autopost-slack-relay.workers.dev/`)가 슬랙 서명을 확인한 뒤
GitHub Actions 를 실행합니다. Worker 가 가진 GitHub 토큰은 YooPost 리포지토리의 Actions 실행 권한 하나뿐입니다 (1년 만료).
같은 Worker 가 **블로그 이미지 저장소**도 맡습니다 (`/img/<이름>`, Cloudflare KV 무료 한도 — 쓰기 하루 1,000회). 업로드는 `IMAGE_UPLOAD_TOKEN` 이 있어야만 됩니다.

---

## 폴더 구조

```
├─ main.py / setup.py         포스팅 실행 / 연동 점검 실행
├─ autopost/
│  ├─ feeds.py · topic.py     주제 후보 수집(RSS·논문·HN) · 주제 선정
│  ├─ gemini.py               Gemini 호출 (모델 대체·재시도·구글 검색 근거 추출)
│  ├─ research.py · links.py  자료 조사(fact sheet) · 출처 주소 확인
│  ├─ quote_images.py         인용 이미지 내려받기·확인
│  ├─ image_host.py           이미지 저장소(Cloudflare Worker) 업로드
│  ├─ writer.py · content.py  원고 작성·수정 · 글 구조(참고 블로그 문법)
│  ├─ style_profile.py        참고 블로그 분석 프로필 → 작성 규칙 (data/style_profile.json)
│  ├─ fact_check.py           주장 추출 → 독립 웹 검색 검증
│  ├─ cover.py                대표 이미지 1200×630
│  ├─ blog_html.py · blog_publisher.py   구글 블로그 HTML(참고 블로그 박스·표 디자인) · Blogger API
│  ├─ publishing.py · preview_page.py    참고문헌·발행 · 미리보기 페이지
│  ├─ blogger_menu.py         블로그 상단 메뉴(카테고리 라벨 페이지) 링크
│  ├─ history.py              발행 기록 = 구글 블로그 글 목록 (중복 주제 방지 · 함께 보면 좋은 글)
│  ├─ slack_helper.py · progress.py · step_summary.py   슬랙 보고 · Actions 요약
│  └─ pipeline.py · cli.py · config.py
├─ scripts/                   토큰 발급·미리보기·참고 블로그 분석·슬랙 채널·시크릿 등록
├─ slack_relay/               슬랙 → GitHub 중계 + 블로그 이미지 저장소 Cloudflare Worker
├─ slack_app_manifest.yml     YooPost 슬랙 앱 한 번에 만들기
├─ .github/workflows/         auto_post.yml(포스팅) · ci.yml(테스트)
└─ tests/                     pytest — 외부 서비스 없이 실행
```

구글 드라이브는 쓰지 않습니다.
- **발행 기록**: 구글 블로그에 올라간 글 목록을 그대로 읽습니다. 글마다 본문 끝에 보이지 않는 주석
  `<!--yoopost {"topic": …, "source": …}-->` 을 남겨 같은 원문을 다시 쓰지 않습니다 (블로그에서 글을 지우면 기록에서도 빠짐).
- **이미지**: 대표 이미지·인용 이미지는 Cloudflare Worker 이미지 저장소에 올려 공개 주소로 넣습니다.
  저장소를 못 쓰면 이미지를 본문에 직접 담아(data URI) 발행은 계속합니다.

---

## 1회 준비

> 계정: 구글 **rkdghkclgns@gmail.com** · 슬랙 워크스페이스 **YooPost** · GitHub **rkdghkclgns-design/YooPost** (비공개)
> 2026-09-29 기준 아래 준비는 모두 끝났습니다. 다시 설치하거나 키를 바꿀 때 참고하세요.

### 0) 슬랙 워크스페이스 YooPost
https://slack.com/get-started#/createnew 에서 워크스페이스를 만들고 이름을 **YooPost** 로 지정합니다.
채널은 3) 의 `setup_slack_channels.py` 가 만듭니다.

### 1) 구글 (블로그 'AI백과사전')
1. rkdghkclgns@gmail.com 으로 https://www.blogger.com → **새 블로그 → 제목 `AI백과사전`**
2. https://console.cloud.google.com → 새 프로젝트 → **Blogger API v3** 사용 설정 (드라이브 권한은 요청하지 않음)
3. `OAuth 동의 화면` → 외부 → **앱 게시(프로덕션)** ※ '테스트' 상태면 refresh token 이 7일 뒤 만료
4. `사용자 인증 정보 → OAuth 클라이언트 ID → 데스크톱 앱` → JSON 을 프로젝트 폴더에 `client_secret.json` 으로 저장
5. 실행 — 로그인 화면에 rkdghkclgns@gmail.com 이 미리 선택되고, 'AI백과사전' 블로그 ID 를 자동으로 찾아 `.env` 에 저장합니다.
   ```bash
   python scripts/get_google_token.py client_secret.json
   ```

6. **상단 메뉴(섹션)** — `홈 | AI 언어 모델 | AI 도구 | AI 논문 분석 | AI 뉴스 리뷰 | AI 보안 | AI 인사이트 | AI 공모주 분석`
   글마다 카테고리가 첫 번째 라벨로 붙으므로 메뉴는 라벨 페이지로 연결합니다. 링크 목록:
   ```bash
   python scripts/blogger_menu.py
   ```
   Blogger → **레이아웃** → 헤더 아래 **가젯 추가 → 페이지** → '홈' 체크 + **외부 링크 추가**로 7개를 위 순서대로 → 저장
   (Blogger API 는 레이아웃을 바꿀 수 없어 이 단계만 화면에서 1번 합니다)
   - 'AI 공모주 분석' 글은 증권신고서·공시의 사실만 정리하고, 투자 권유가 아니라는 안내가 항상 붙습니다.

### 2) Gemini (YooPost 전용 프로젝트)
**평소 쓰는 크롬에서** https://aistudio.google.com/apikey → API 키 만들기 → 프로젝트 만들기 `YooPost` → 키 만들기 → 복사
(자동화 브라우저에서는 구글이 프로젝트·키 생성을 막습니다)
```bash
python scripts/import_pasted_secret.py GEMINI_API_KEY --open   # 메모장이 열리면 붙여넣고 Ctrl+S
python scripts/import_pasted_secret.py GEMINI_API_KEY          # .env + GitHub 에 저장, 파일 삭제
```
> 자료 조사·사실 확인에 **구글 검색 근거(Grounding with Google Search)** 를 씁니다 (유료 딥리서치는 쓰지 않음).
> 무료 한도는 **모델당 하루 20회**라 다른 앱과 같은 프로젝트를 쓰면 한도를 나눠 쓰게 됩니다 — 전용 프로젝트를 쓰세요. 키 형식은 `AIza…` 또는 `AQ.…`.

### 3) 슬랙 앱
1. https://api.slack.com/apps → **Create New App → From an app manifest** → 워크스페이스 **YooPost** → `slack_app_manifest.yml` 붙여넣기 → Create and Install
2. `OAuth & Permissions` 의 **Bot User OAuth Token(xoxb-...)** 복사 →
   `python scripts/import_pasted_secret.py SLACK_BOT_TOKEN --open` 로 붙여넣은 뒤 `python scripts/import_pasted_secret.py SLACK_BOT_TOKEN`
3. 채널 4개 만들기·나를 초대·채널 ID 저장(.env + GitHub):
   ```bash
   python scripts/setup_slack_channels.py
   ```
4. 연결 확인: `python scripts/slack_test.py`

### 4) GitHub 리포지토리 YooPost + 시크릿
```bash
python scripts/push_secrets.py          # .env 의 비밀값 → Secrets, 일반 설정 → Variables
```
(리포지토리는 비공개 `rkdghkclgns-design/YooPost`. 공개로 바꾸지 마세요)

### 5) Cloudflare Worker (슬랙 `/aipost` 명령 중계)
```bash
cd slack_relay && npx wrangler deploy && cd ..        # (wrangler login 이 되어 있어야 함: npx wrangler whoami)
python scripts/relay_secrets.py --open                # 메모장 2개: Signing Secret, GitHub 토큰 붙여넣고 Ctrl+S
python scripts/relay_secrets.py                       # Worker 에 등록 (값은 출력·저장하지 않고 파일 삭제)
```
- **Signing Secret**: 슬랙 앱 `Basic Information → App Credentials → Signing Secret → Show`
- **GitHub 토큰**: `Settings → Developer settings → Fine-grained tokens` → Repository access **Only select → YooPost** →
  Permissions **Actions: Read and write** 하나 → Generate (만료 1년 — 만료되면 새로 만들어 `relay_secrets.py` 다시 실행)
- 배포 주소를 슬랙 앱 `Slash Commands` 두 개의 Request URL 에 넣습니다 (현재 `https://yoopost-slack-relay.cleaning-autopost-slack-relay.workers.dev/`).
- (선택) `wrangler.toml` 의 `ALLOWED_USER_IDS` 에 실행을 허용할 슬랙 사용자 ID
- **이미지 저장소**: `wrangler.toml` 의 KV `IMAGES` 바인딩 + 업로드 비밀값 (한 번에 .env·Worker·GitHub 등록)
  ```bash
  python scripts/image_host_secret.py        # 바꾸려면 --rotate
  ```

### 6) 연동 점검
슬랙 `/aipost-setup` (또는 GitHub `Actions → AI백과사전 자동 포스팅 → Run workflow → mode: setup`)
→ 구글 블로그·Gemini·이미지 저장소·슬랙을 실제로 연결해 보고 **"✅ 연동 점검 완료" + 점검표**를 보냅니다.

---

## 운영

- 매일 **11:00(KST)** 자동 실행 (GitHub 서버 — PC 가 꺼져 있어도 됨, 시작이 수~수십 분 늦을 수 있음)
  - Variable `AUTO_PUBLISH=true` (**2026-09-29 설정됨**): 바로 **자동 발행**
  - 비우거나 `false`: **검토용 미리보기**만 만들어 슬랙에 올림 → 확인 후 `/aipost` 로 발행
- 발행된 글은 곧 발행 기록 → 같은 주제는 다시 고르지 않고, 다음 글의 '함께 보면 좋은 글'에 연결됩니다.
- 사실 검증 미통과·발행 실패·테스트 실행은 블로그에 올라가지 않으므로 다음 실행에서 다시 주제를 고릅니다.
- 한 번 실행에 보통 **5~15분** 걸립니다 (AI 과부하 때 재시도하면 더 걸림). Actions 제한 시간은 40분.
- 사실 검증을 끝내지 못하면(AI 과부하 등) 써 둔 원고를 **'사실 검증 전 원고'**로 원고 채널에 남기고 발행하지 않습니다.

### 내 PC에서 미리보기 (아무것도 올리지 않음, GEMINI_API_KEY 만 필요)
```bash
python scripts/preview_post.py                   # 주제 자동 선정
python scripts/preview_post.py --topic "GPT-6"   # 주제 지정
```
`output/preview_…/` 에 `sources.md`, `quote_I*.jpg`(인용 이미지), `draft_unverified.txt`(검증 전), `post.txt`, `factcheck.md`,
`cover.png`, `blog_preview_google.html`(구글 블로그에 올라갈 모습) 이 만들어집니다.

### 참고 블로그 문법 다시 분석
```bash
python scripts/analyze_reference_blog.py   # 새 글만 내려받아 style_profile.json 갱신 (캐시: output/reference_cache)
```

## 로컬 실행 / 테스트
```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env
python setup.py                    # 연동 점검
python main.py --dry-run           # 발행 없이 미리보기 (슬랙으로)
python main.py --topic "GPT-6"     # 실제 포스팅

pytest --cov=autopost              # 파이썬 테스트 (외부 서비스 불필요)
cd slack_relay && node --test      # Worker 테스트
```

## 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| `자료 조사·출처 확인` 실패 "확인된 자료가 부족합니다" | 주제가 너무 새로워 공식 자료가 아직 적음 → 다음 실행이 다른 주제를 고름. `MIN_REFERENCES` 를 낮출 수 있음 |
| 이미지가 `data:image/...` 로 본문에 들어감 | 이미지 저장소 업로드 실패 → `/aipost-setup` 점검, `python scripts/image_host_secret.py` 로 비밀값 다시 등록 |
| ✂️ 문제 문장·항목 N곳 빼고 발행 | 정상 동작 — 뺀 문장은 원고채널 `factcheck.md` 끝 '발행 전 제외한 문장·항목'에서 확인 |
| ⚠️ 발행 보류 (위치 못 찾음·본문이 너무 짧아짐) | 원고채널 `factcheck.md` 확인. `/aipost` 로 다른 주제 실행 |
| `refresh token 이 만료` | OAuth 동의화면이 '테스트' 상태 → 프로덕션 게시 후 `get_google_token.py` 재실행 |
| Gemini 429/503 | 하루 한도를 다 쓴 모델은 그 실행에서 건너뛰고, 과부하는 30·60·90초 기다려 재시도. 계속되면 전용 프로젝트·결제 연결 |
| 슬랙 명령 후 `GitHub API HTTP 401/403/404` | Worker 의 GitHub 토큰 만료·권한(Actions: Read and write)·대상 리포지토리(YooPost) 확인 → 새 토큰으로 `relay_secrets.py` |
| 슬랙 명령 후 `dispatch_failed` | 슬랙 앱 Slash Commands 의 Request URL 이 Worker 주소인지 확인 |
| 대표 이미지 글자가 네모(□) | 한글 폰트 없음. 로컬은 `FONT_PATH` 지정, Actions 는 `fonts-nanum` 자동 설치 |

## 참고 / 주의
- AI 원고는 사실 검증을 거치지만, **처음 한동안은 발행 전 미리보기를 직접 확인**하세요.
- 참고 블로그는 글 문법 분석에만 쓰며, 그 블로그의 글·이미지를 옮기지 않습니다. 대표 이미지는 직접 생성합니다.
- 인용 이미지는 조사한 **공식 출처의 이미지만**, 본문 설명을 돕는 범위에서 **출처 링크와 함께** 씁니다.
- 하루 여러 건 발행은 스팸 판정 위험이 있어 하루 1편을 권장합니다.
