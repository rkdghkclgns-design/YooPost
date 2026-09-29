# yoopost-drafts — 클라우드 원고 데이터 (코드 없음)

매일 07:00(KST) 클라우드 루틴(Claude)이 카테고리별 원고를 이 브랜치에 올립니다.
push 가 오면 `.github/workflows/cloud_drafts.yml` 이 main 의 코드로 슬랙 승인 요청을 보냅니다 (비밀값은 GitHub 에만).

| 경로 | 내용 | 쓰는 쪽 |
|---|---|---|
| `drafts/<YYYYMMDD_HHMMSS_n>/post.json`·`research.json` | 원고 (n = 카테고리 번호, 폴더 이름 = 원고 ID) | 클라우드 |
| `drafts/<…_n>/skip.json` | 건너뛴 카테고리와 이유 | 클라우드 |
| `runs/<YYYYMMDD>.json` | 실행 시작·끝, 네트워크 상태 | 클라우드 |
| `state/progress_<YYYYMMDD>.json` | 슬랙 진행 막대 메시지·요약 전송 여부 | Actions |
| `state/failures.json` | 슬랙 승인 요청으로 보내지 못한 원고와 이유 | Actions |
| `state/published.json` | 발행 기록 (주제 중복 방지용) | Actions |

발행은 슬랙 원고 메시지에 ✅ 를 눌러야만 됩니다.
