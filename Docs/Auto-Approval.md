# YooPost 60초 자동 승인

사용자 요청(2026-10-06)에 따라 원고 승인 정책을 변경했다.

- `#yoopost-1-원고승인` 원고 메시지에 ✅를 누르면 즉시 승인한다.
- 메시지가 게시된 지 60초가 지나도록 미승인이면 Cloudflare가 자동 승인하고 GitHub `publish_draft.yml`을 요청한다.
- 미리보기·대표 이미지·출처 업로드와 원고 연결이 끝나야 타이머를 등록한다. 업로드가 60초 넘게 걸리면 연결 직후 자동 승인한다.
- Slack 메시지를 게시하자마자 원고를 먼저 연결해 첨부 업로드 중에도 ❌ 보류를 처리한다. 자동 승인은 `approval_ready`가 참이 된 뒤 시작한다.
- 60초 안에 허용된 사용자가 ❌를 누르면 `held` 상태로 보류하고 타이머를 해제한다. 이후 ✅로 직접 승인할 수 있다.
- 자동 승인 시 `yoopost` 봇이 ✅ 반응과 스레드 안내를 남긴다. 발행 완료 메시지는 자동 승인임을 표시한다.
- 승인 요청 이후 실제 발행 시간은 GitHub 실행 대기 시간에 따라 더 걸린다.

## PC 종료 시 동작

원고 하나마다 SQLite 기반 `DraftApproval` Durable Object를 사용한다. 타이머는 `setAlarm()`으로 Cloudflare에 저장되며 PC나 Codex 데스크톱 실행에 의존하지 않는다. 사람이 누르는 승인, 알람, 발행 상태 저장을 같은 객체에서 직렬 처리한다. 큰 원고 HTML은 기존 `IMAGES` KV에 보관하고, 최신 승인 상태는 Durable Object가 관리한다.

원고가 새로 보관되면서 Slack 메시지와 연결될 때 타이머를 등록한다. 배포 전에 보관된 원고는 다시 연결 저장하기 전까지 기존 수동 승인 상태를 유지한다. 이미 승인·발행·보류한 원고를 과거 `pending` 기록으로 덮어써도 자동 승인되지 않는다.

발행 요청 실패는 60초 간격으로 최대 5회 시도하고 멈춘다. 이후 ✅로 다시 시도할 수 있다. 실제 Blogger 발행 실패는 자동 재승인하지 않는다. 기존 발행 직전 중복 검사·사실 검증·출처·이미지 검사를 유지한다.

## 배포와 확인

`slack_relay/wrangler.toml`에 다음이 포함되어야 한다.

- `AUTO_APPROVE_ENABLED = "true"`
- `DRAFT_APPROVAL` 바인딩과 `DraftApproval` 클래스
- `v1-draft-approval` SQLite 클래스 생성 마이그레이션

Cloudflare 로그인 후 `slack_relay`에서 `npx wrangler deploy`를 실행한다. 기존 Worker의 Slack/GitHub 비밀값은 유지한다. `/health`가 `auto_approve: true`, `auto_approve_seconds: 60`, `durable_approval: true`를 반환하는지 확인한다. 인증된 `/draft/<ID>` 읽기에서 `auto_approve_at`, `approved_by`, `status`를 확인할 수 있다.

자동 승인을 끄려면 `AUTO_APPROVE_ENABLED = "false"`로 배포한다. 대기 알람은 발행 요청 없이 해제된다. 이미 GitHub에 전달된 발행 요청은 별도 실행이므로 영향을 받지 않는다.

## 검증

`slack_relay/approval.test.js`는 60초 경계, 재시작 후 타이머, 수동/자동 동시 승인, ❌ 보류와 재승인, 게시 시각 기준, 상태 덮어쓰기 방지, 실패 재시도 상한을 검증한다. 기존 Worker 이벤트 테스트도 함께 실행한다. Python 발행 테스트는 자동 승인자를 기록하고 실제 사람의 Slack ID로 잘못 표시하지 않는지 확인한다.

Cloudflare의 [알람 실행 규칙](https://developers.cloudflare.com/durable-objects/api/alarms/)에 따라 타이머는 60초 이후 실행되며 서비스 장애 시 지연될 수 있다.
