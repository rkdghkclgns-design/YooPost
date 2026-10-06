/**
 * 슬랙(YooPost 워크스페이스) ↔ GitHub Actions 중계 + 블로그 이미지·원고 보관 Cloudflare Worker.
 *
 * 원고는 Claude(데스크톱 앱)가 쓴다 — 구글 AI API 를 쓰지 않는다.
 *   Claude → PUT /draft/<ID> (원고 보관) → 슬랙 원고 채널에 '✅ 누르면 발행' 메시지
 *   사용자가 그 메시지에 ✅ 반응 또는 60초 미승인 → Durable Object 가 직렬 처리
 *   → GitHub Actions publish_draft.yml 실행 → 구글 블로그 발행 → 슬랙 스레드에 결과
 * GitHub Actions 는 외부 요청을 직접 받을 수 없어서 이 Worker 가 서명 검증 후 대신 실행시킨다. (Cloudflare 무료 플랜)
 *
 * 필요한 설정 (wrangler secret put / wrangler.toml [vars])
 *   SLACK_SIGNING_SECRET : 슬랙 앱 > Basic Information > Signing Secret
 *   GITHUB_TOKEN         : fine-grained PAT (YooPost 리포지토리 Actions: Read and write 만)
 *   GITHUB_REPO          : "깃허브아이디/YooPost"
 *   GITHUB_WORKFLOW      : (선택) 워크플로 파일명, 기본 auto_post.yml
 *   GITHUB_REF           : (선택) 실행 브랜치, 기본 main
 *   ALLOWED_USER_IDS     : (선택) 실행 허용 슬랙 사용자 ID 목록 "U0123,U0456"
 *   IMAGE_UPLOAD_TOKEN   : 블로그 이미지 업로드용 비밀값 (GitHub Actions 와 같은 값)
 *   IMAGES               : 이미지·원고 저장 KV 바인딩 (wrangler.toml [[kv_namespaces]])
 *   PUBLISH_WORKFLOW     : (선택) ✅ 승인 시 실행할 워크플로, 기본 publish_draft.yml
 *   APPROVE_REACTIONS    : (선택) 승인으로 볼 반응 이름, 기본 "white_check_mark,heavy_check_mark"
 *   MANUAL_CHANNEL_ID    : 수동 지시 채널(#yoopost-0-수동지시) ID — 비우면 채널 메시지를 받지 않는다
 *   MANUAL_WORKFLOW      : (선택) 수동 지시 워크플로, 기본 manual_request.yml
 *   SLACK_BOT_TOKEN      : (선택) 수동 지시를 GitHub 로 넘기지 못했을 때 그 스레드에 알리는 용도
 *
 * 수동 지시: #yoopost-0-수동지시 에 "주제를 AI도구에 발행해줘" · "전체 새로운 포스트로 재발행" 을 쓰면
 *   (슬랙 이벤트 message.channels) → manual_request.yml 실행 → 클라우드 루틴이 원고 작성 → 원고 채널에서 ✅ 로 발행.
 *   /aipost <지시> 도 같은 지시로 처리한다 (앱에 channels:history 권한이 없어도 쓸 수 있는 길).
 *
 * 슬랙 명령:  /aipost <지시> · /aipost 도움말 → 사용법 · /aipost-setup → 연동 상태 점검
 *   (/aipost 로 Gemini 가 글을 쓰던 기능은 껐다 — 원고는 Claude 가 쓴다)
 *
 * 블로그 이미지 호스팅 (구글 드라이브 대신):
 *   PUT /img/<이름>  (Authorization: Bearer IMAGE_UPLOAD_TOKEN) → KV 에 저장
 *   GET /img/<이름>  → 누구나 볼 수 있는 이미지 주소 (구글 블로그 본문 <img> 에 사용)
 * 원고 보관함 (둘 다 Authorization: Bearer IMAGE_UPLOAD_TOKEN 필요):
 *   PUT /draft/<ID>  원고 저장 (slack.channel·slack.ts 가 있으면 그 메시지의 ✅ 와 연결)
 *   GET /draft/<ID>  원고 읽기 (발행 워크플로가 사용)
 */

export const COMMANDS = {
  "/aipost": "post",
  "/ai포스팅": "post",
  "/aipost-setup": "setup",
  "/초기세팅": "setup",
};

const MAX_AGE_SECONDS = 60 * 5;
const MAX_TOPIC = 100;
const IMAGE_PATH = /^\/img\/([A-Za-z0-9][A-Za-z0-9._-]{0,119})$/;
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const DRY_RUN_WORDS = /(테스트|dry-?run|test)/gi;
const HELP_WORDS = /^(도움말|help|사용법|\?)$/i;
const ALL_WORDS = /(^|\s)(전체|카테고리별|all)(?=\s|$)/gi;
const DRAFT_PATH = /^\/draft\/([A-Za-z0-9][A-Za-z0-9._-]{0,119})$/;
const MAX_DRAFT_BYTES = 2 * 1024 * 1024;
const RETRYABLE = new Set(["pending", "failed"]);
const MAX_MANUAL_TEXT = 500;
const MANUAL_SEEN_SECONDS = 60 * 60 * 24 * 3; // 같은 슬랙 메시지를 두 번 처리하지 않게 3일 기억

export const WRITE_NOTICE = [
  "✍️ 원고는 *Claude* 가 쓰고, 사용량 한도 등으로 중단되면 *Codex 클라우드*가 이어받습니다. PC가 꺼져 있어도 Codex 인계·작성·승인 후 발행은 계속됩니다.",
  "더 쓰고 싶으면 *#yoopost-0-수동지시* 채널에 한 줄로 지시하세요 (또는 `/aipost <지시>`):",
  "  • `슬랙에 에이전트 직원 만들기를 AI도구에 발행해줘` · `AI 보안 한 편 더 발행해줘` · `전체 새로운 포스트로 재발행`",
  "원고는 사실 검증 뒤 원고 채널에 올라옵니다. ✅ 를 누르면 즉시 승인, 1분 동안 미승인이면 자동 승인됩니다. ❌ 를 누르면 보류합니다.",
].join("\n");

export const HELP_TEXT = [
  "*🤖 AI백과사전 사용법*",
  WRITE_NOTICE,
  "",
  "• 원고 메시지에 ✅ — 즉시 승인 · 1분 미승인 — 자동 승인 · ❌ — 보류 (결과는 스레드에)",
  "• `/aipost-setup` — 구글 블로그·이미지 저장소 연동 상태 점검",
].join("\n");

export default {
  async fetch(request, env, ctx) {
    const path = new URL(request.url).pathname;
    if (path === "/health" && request.method === "GET") {
      return json({ ok: true, auto_approve: autoApprovalEnabled(env),
        auto_approve_seconds: 60, durable_approval: Boolean(env.DRAFT_APPROVAL) });
    }
    const image = path.match(IMAGE_PATH);
    if (image) {
      return handleImage(request, env, image[1]);
    }
    const draft = path.match(DRAFT_PATH);
    if (draft) {
      return handleDraft(request, env, draft[1]);
    }
    if (request.method !== "POST") {
      return new Response("ok");
    }
    const raw = await request.text();
    if (!(await verifySlackSignature(request.headers, raw, env.SLACK_SIGNING_SECRET))) {
      return new Response("invalid signature", { status: 401 });
    }
    if (path === "/slack/events") {
      return handleSlackEvent(request, env, ctx, raw);
    }

    const form = new URLSearchParams(raw);
    const command = form.get("command") || "";
    const mode = COMMANDS[command];
    if (!mode) {
      return reply(`알 수 없는 명령어입니다: ${command}\n\n${HELP_TEXT}`);
    }
    const text = (form.get("text") || "").trim();
    if (HELP_WORDS.test(text)) {
      return reply(HELP_TEXT);
    }

    const userId = form.get("user_id") || "";
    const allowed = (env.ALLOWED_USER_IDS || "").split(",").map((s) => s.trim()).filter(Boolean);
    if (allowed.length > 0 && !allowed.includes(userId)) {
      return reply("이 명령어를 실행할 권한이 없습니다. 관리자에게 문의하세요.");
    }

    if (mode === "post") {
      // Gemini 가 글을 쓰던 기능은 꺼 둠 — 지시가 있으면 수동 지시(클라우드 루틴 Claude)로 넘긴다
      if (!text) {
        return reply(WRITE_NOTICE);
      }
      if (!allowedUser(env, userId, { requireList: true })) {
        return reply("수동 지시는 허용된 사용자만 쓸 수 있습니다 (Worker 의 ALLOWED_USER_IDS).");
      }
      try {
        await dispatch(env, manualInputs(text, form.get("channel_id") || "", "", userId, "slash"), manualWorkflow(env));
      } catch (err) {
        return reply(`수동 지시를 넘기지 못했습니다: ${err.message}`);
      }
      return reply("🧑‍💼 지시를 접수했습니다. #yoopost-0-수동지시 스레드에서 진행 상황을 알려 드립니다.");
    }
    const { topic, dryRun, all } = parseText(text);
    const inputs = buildInputs(mode, topic, dryRun, cleanText(form.get("user_name") || userId, 30), all);
    try {
      await dispatch(env, inputs);
    } catch (err) {
      return reply(`GitHub Actions 실행 요청에 실패했습니다: ${err.message}`);
    }

    const label = mode === "setup"
      ? "연동 상태 점검"
      : all
        ? `카테고리별 포스팅 7편${dryRun ? " [테스트]" : ""} (차례로 30~90분)`
        : `AI 포스팅${topic ? ` (${topic})` : ""}${dryRun ? " [테스트]" : ""}`;
    return reply(
      `🤖 ${label} 요청을 접수했습니다. 1분 안에 진행 메시지가 올라오고, 단계마다 갱신됩니다.`,
      "in_channel",
    );
  },
};

function authorized(request, env) {
  const expected = env.IMAGE_UPLOAD_TOKEN ? `Bearer ${env.IMAGE_UPLOAD_TOKEN}` : "";
  return Boolean(expected) && timingSafeEqual(expected, request.headers.get("Authorization") || "");
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

export async function handleDraft(request, env, id) {
  if (!env.IMAGES) {
    return new Response("draft store not configured", { status: 503 });
  }
  if (!authorized(request, env)) {
    return new Response("unauthorized", { status: 401 });
  }
  if (env.DRAFT_APPROVAL) {
    return approvalStub(env, id).fetch(request);
  }
  if (autoApprovalEnabled(env)) {
    return new Response("durable approval not configured", { status: 503 });
  }
  if (request.method === "GET") {
    const draft = await env.IMAGES.get(`draft:${id}`, { type: "json" });
    return draft ? json(draft) : new Response("not found", { status: 404 });
  }
  if (request.method !== "PUT") {
    return new Response("method not allowed", { status: 405, headers: { Allow: "GET, PUT" } });
  }
  const raw = await request.text();
  if (raw.length === 0 || raw.length > MAX_DRAFT_BYTES) {
    return new Response("draft too large or empty", { status: 413 });
  }
  let draft;
  try {
    draft = JSON.parse(raw);
  } catch {
    return new Response("invalid json", { status: 400 });
  }
  if (!draft || typeof draft !== "object" || !draft.title || !draft.html) {
    return new Response("draft needs title and html", { status: 400 });
  }
  await env.IMAGES.put(`draft:${id}`, JSON.stringify({ ...draft, id }));
  const slack = draft.slack || {};
  if (slack.channel && slack.ts) {
    await env.IMAGES.put(`msg:${slack.channel}:${slack.ts}`, id); // 이 메시지의 ✅ → 이 원고
  }
  return json({ ok: true, id }, 201);
}

export async function handleSlackEvent(request, env, ctx, raw) {
  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    return new Response("invalid json", { status: 400 });
  }
  if (body.type === "url_verification") {
    return json({ challenge: body.challenge }); // 슬랙 앱 이벤트 구독 주소 확인
  }
  if (request.headers.get("X-Slack-Retry-Num")) {
    return new Response("ok"); // 재전송은 무시 (원고 상태로도 중복 발행을 막음)
  }
  const event = body.event || {};
  let work = null;
  if (body.type === "event_callback" && event.type === "reaction_added"
      && (isApproval(event.reaction, env) || isHold(event.reaction))) {
    work = approveDraft(env, event).catch((err) => console.error("approve failed", err.message));
  } else if (body.type === "event_callback" && event.type === "message") {
    work = manualMessage(env, event).catch((err) => console.error("manual request failed", err.message));
  }
  if (work && ctx && ctx.waitUntil) {
    ctx.waitUntil(work); // 슬랙에는 3초 안에 바로 응답
  } else if (work) {
    await work;
  }
  return new Response("ok");
}

function allowedUser(env, user, { requireList = false } = {}) {
  const allowed = (env.ALLOWED_USER_IDS || "").split(",").map((s) => s.trim()).filter(Boolean);
  if (allowed.length === 0) {
    return !requireList; // 수동 지시는 유료 루틴을 돌리므로 허용 목록이 비어 있으면 아무도 못 쓴다
  }
  return allowed.includes(user);
}

function manualWorkflow(env) {
  return env.MANUAL_WORKFLOW || "manual_request.yml";
}

export function manualInputs(text, channel, ts, user, source) {
  // 해석은 Actions(Python)가 한다 — 여기서는 길이만 자르고 그대로 넘긴다 (입력은 워크플로에서 환경 변수로만 읽는다)
  return {
    text: String(text || "").trim().slice(0, MAX_MANUAL_TEXT),
    channel: String(channel || ""),
    ts: String(ts || ""),
    user: String(user || ""),
    source,
  };
}

/** #yoopost-0-수동지시 의 사람이 쓴 새 메시지(스레드 답글·수정·봇 제외) → manual_request.yml */
export async function manualMessage(env, event) {
  if (!env.MANUAL_CHANNEL_ID || event.channel !== env.MANUAL_CHANNEL_ID) {
    return "ignored";
  }
  // 파일을 붙인 지시(file_share)는 글만 지시로 쓴다. 수정·삭제·봇 글 등 다른 subtype 은 무시
  if ((event.subtype && event.subtype !== "file_share") || event.bot_id || !event.user || !event.ts) {
    return "ignored";
  }
  if (event.thread_ts && event.thread_ts !== event.ts) {
    return "thread reply";
  }
  if (!String(event.text || "").trim()) {
    return "blank";
  }
  if (!allowedUser(env, event.user, { requireList: true })) {
    return "not allowed";
  }
  const seenKey = `manual:${event.channel}:${event.ts}`;
  if (env.IMAGES) {
    if (await env.IMAGES.get(seenKey)) {
      return "duplicate";
    }
    await env.IMAGES.put(seenKey, "1", { expirationTtl: MANUAL_SEEN_SECONDS });
  }
  try {
    await dispatch(env, manualInputs(event.text, event.channel, event.ts, event.user, "message"), manualWorkflow(env));
  } catch (err) {
    if (env.IMAGES && env.IMAGES.delete) {
      await env.IMAGES.delete(seenKey); // 다시 보내면 재시도할 수 있게
    }
    await notifySlack(env, event.channel, event.ts,
      `⚠️ 지시를 GitHub Actions 로 넘기지 못했습니다 (${cleanText(err.message, 120)}) — 잠시 뒤 같은 지시를 새로 보내 주세요.`);
    throw err;
  }
  return "dispatched";
}

/** (선택) SLACK_BOT_TOKEN 이 있으면 지시 스레드에 한 줄 알린다 — 실패해도 조용히 넘어간다 */
export async function notifySlack(env, channel, threadTs, text) {
  if (!env.SLACK_BOT_TOKEN || !channel) {
    return false;
  }
  try {
    const response = await fetch("https://slack.com/api/chat.postMessage", {
      method: "POST",
      headers: { Authorization: `Bearer ${env.SLACK_BOT_TOKEN}`, "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ channel, thread_ts: threadTs, text }),
      signal: AbortSignal.timeout(5000),
    });
    return response.ok && Boolean((await response.json()).ok);
  } catch (err) {
    console.error("slack notice failed", err.message);
    return false;
  }
}

function isApproval(reaction, env) {
  const names = (env.APPROVE_REACTIONS || "white_check_mark,heavy_check_mark").split(",").map((s) => s.trim());
  return names.includes(String(reaction || "").split("::")[0]);
}

function isHold(reaction) {
  return String(reaction || "").split("::")[0] === "x";
}

function autoApprovalEnabled(env) {
  return String(env.AUTO_APPROVE_ENABLED || "false").toLowerCase() === "true";
}

function approvalStub(env, id) {
  return env.DRAFT_APPROVAL.get(env.DRAFT_APPROVAL.idFromName(id));
}

export async function approveDraft(env, event) {
  const item = event.item || {};
  if (!env.IMAGES || item.type !== "message") {
    return "ignored";
  }
  const id = await env.IMAGES.get(`msg:${item.channel}:${item.ts}`);
  if (!id) {
    return "not a draft";
  }
  if (!allowedUser(env, event.user)) return "not allowed";
  if (env.DRAFT_APPROVAL) {
    const response = await approvalStub(env, id).fetch(new Request(`https://approval.internal/draft/${id}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: isHold(event.reaction) ? "hold" : "approve", user: event.user }),
    }));
    if (!response.ok) throw new Error(`approval HTTP ${response.status}`);
    return (await response.json()).result;
  }
  if (isHold(event.reaction)) return "ignored";
  const draft = await env.IMAGES.get(`draft:${id}`, { type: "json" });
  if (!draft || !RETRYABLE.has(draft.status || "pending")) {
    return "already handled";
  }
  if (!allowedUser(env, event.user)) {
    return "not allowed";
  }
  await env.IMAGES.put(`draft:${id}`, JSON.stringify({ ...draft, status: "approved", approved_by: event.user }));
  try {
    await dispatch(env, { draft_id: id, approved_by: String(event.user || "") },
      env.PUBLISH_WORKFLOW || "publish_draft.yml");
  } catch (err) {
    await env.IMAGES.put(`draft:${id}`, JSON.stringify({ ...draft, status: "pending" })); // 다시 ✅ 로 재시도
    throw err;
  }
  return "dispatched";
}

/** 원고 하나당 영속 타이머. 수동 승인·자동 승인·상태 쓰기를 같은 객체에서 차례로 처리한다. */
export class DraftApproval {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
  }

  async fetch(request) {
    const id = new URL(request.url).pathname.match(DRAFT_PATH)?.[1];
    if (!id) return new Response("not found", { status: 404 });
    return this.ctx.blockConcurrencyWhile(async () => {
      const record = await this.ctx.storage.get("approval");
      if (request.method === "GET") {
        const draft = await this.env.IMAGES.get(`draft:${id}`, { type: "json" });
        return draft ? json({ ...draft, ...(record || {}) }) : new Response("not found", { status: 404 });
      }
      if (request.method === "PUT") return this.save(request, id, record);
      if (request.method === "POST") {
        const input = await request.json();
        if (!allowedUser(this.env, input.user)) return json({ result: "not allowed" }, 403);
        const draft = await this.env.IMAGES.get(`draft:${id}`, { type: "json" });
        if (!draft) return json({ result: "not a draft" }, 404);
        const current = record || { id, status: draft.status || "pending" };
        if (input.action === "hold") {
          if (!["pending", "failed", "held"].includes(current.status)) return json({ result: "already handled" });
          await this.write(draft, { ...current, status: "held", held_by: input.user,
            auto_approve_at: null, dispatch_pending: false });
          await this.ctx.storage.deleteAlarm();
          await notifySlack(this.env, draft.slack?.channel, draft.slack?.ts,
            "⏸️ 원고를 보류했습니다. 자동 승인하지 않습니다. 다시 ✅ 를 누르면 승인합니다.");
          return json({ result: "held" });
        }
        if (input.action !== "approve") return new Response("invalid action", { status: 400 });
        if (!["pending", "failed", "held"].includes(current.status)) return json({ result: "already handled" });
        await this.publish(draft, { ...current, auto_attempts: 0 }, input.user);
        return json({ result: "dispatched" });
      }
      return new Response("method not allowed", { status: 405 });
    });
  }

  async save(request, id, record) {
    const raw = await request.text();
    if (!raw || raw.length > MAX_DRAFT_BYTES) return new Response("draft too large or empty", { status: 413 });
    let draft;
    try { draft = JSON.parse(raw); } catch { return new Response("invalid json", { status: 400 }); }
    if (!draft || !draft.title || !draft.html) return new Response("draft needs title and html", { status: 400 });
    const status = draft.status || "pending";
    // 승인 요청의 마지막 저장/재전송이 승인·발행·보류 상태를 pending 으로 덮지 않는다.
    const keep = status === "pending" && record && record.status !== "pending";
    if (keep) draft = (await this.env.IMAGES.get(`draft:${id}`, { type: "json" })) || draft;
    draft.slack ||= record?.slack;
    const next = { ...(record || {}), id, status: keep ? record.status : status };
    if (draft.slack?.channel && draft.slack?.ts) next.slack = draft.slack;
    if (!keep) {
      next.approved_by = draft.approved_by || next.approved_by || "";
      next.dispatch_pending = false;
    }
    for (const key of ["url", "published_at", "excluded_reason"]) {
      if (draft[key] !== undefined) next[key] = draft[key];
    }
    if (next.status === "pending" && draft.approval_ready !== false
        && draft.slack?.channel && draft.slack?.ts && autoApprovalEnabled(this.env)) {
      // Slack 메시지 게시 후 60초. 업로드가 오래 걸렸으면 미리보기 연결이 끝난 뒤 즉시 승인한다.
      const postedAt = Number(draft.slack.ts) * 1000;
      const startedAt = Number.isFinite(postedAt) && postedAt > 0 && postedAt <= Date.now() ? postedAt : Date.now();
      next.auto_approve_at ||= new Date(Math.max(Date.now(), startedAt + 60_000)).toISOString();
      await this.ctx.storage.setAlarm(Date.parse(next.auto_approve_at));
    } else if (!next.dispatch_pending) {
      next.auto_approve_at = null;
      await this.ctx.storage.deleteAlarm();
    }
    await this.write(draft, next);
    if (draft.slack?.channel && draft.slack?.ts) {
      await this.env.IMAGES.put(`msg:${draft.slack.channel}:${draft.slack.ts}`, id);
    }
    return json({ ok: true, id, auto_approve_at: next.auto_approve_at || null }, 201);
  }

  async write(draft, record) {
    // 큰 HTML 은 KV 에, 작은 승인 상태는 강한 일관성을 보장하는 Durable Object 에 보관한다.
    await this.ctx.storage.put("approval", record);
    await this.env.IMAGES.put(`draft:${record.id}`, JSON.stringify({ ...draft, ...record }));
  }

  async publish(draft, record, user) {
    const approved = { ...record, status: "approved", approved_by: user,
      approved_at: new Date(Date.now()).toISOString(), dispatch_pending: true };
    // 요청 도중 객체가 재시작되어도 다음 알람에서 복구한다. 발행 워크플로도 원고 ID 로 중복을 막는다.
    await this.write(draft, approved);
    await this.ctx.storage.setAlarm(Date.now() + 60_000);
    try {
      await dispatch(this.env, { draft_id: record.id, approved_by: user }, this.env.PUBLISH_WORKFLOW || "publish_draft.yml");
    } catch (err) {
      await this.write(draft, { ...record, status: "pending", dispatch_pending: false });
      throw err;
    }
    await this.write(draft, { ...approved, dispatch_pending: false, auto_approve_at: null });
    await this.ctx.storage.deleteAlarm();
  }

  async alarm() {
    return this.ctx.blockConcurrencyWhile(async () => {
      let record = await this.ctx.storage.get("approval");
      if (!record) return;
      if ((record.auto_attempts || 0) >= 5) {
        await this.ctx.storage.deleteAlarm();
        return;
      }
      const recovering = record.status === "approved" && record.dispatch_pending;
      if (!recovering && (record.status !== "pending" || !record.auto_approve_at || !autoApprovalEnabled(this.env))) {
        await this.ctx.storage.deleteAlarm();
        return;
      }
      if (!recovering && Date.parse(record.auto_approve_at) > Date.now()) {
        await this.ctx.storage.setAlarm(Date.parse(record.auto_approve_at));
        return;
      }
      const draft = await this.env.IMAGES.get(`draft:${record.id}`, { type: "json" });
      if (!draft) {
        record = { ...record, auto_attempts: (record.auto_attempts || 0) + 1 };
        await this.ctx.storage.put("approval", record);
        if (record.auto_attempts < 5) await this.ctx.storage.setAlarm(Date.now() + 60_000);
        else await this.ctx.storage.deleteAlarm();
        return;
      }
      if (!draft.slack?.channel || !draft.slack?.ts) {
        await this.ctx.storage.deleteAlarm();
        return;
      }
      record = { ...record, auto_attempts: (record.auto_attempts || 0) + 1 };
      try {
        const user = recovering ? record.approved_by : "auto-60s";
        await this.publish(draft, record, user);
        if (user === "auto-60s") {
          await notifySlack(this.env, draft.slack.channel, draft.slack.ts,
            "🤖✅ 자동 승인 (1분 경과) — 구글 블로그 발행을 요청했습니다.");
          await autoReaction(this.env, draft.slack);
        }
      } catch (err) {
        console.error("auto approval dispatch failed", err.message);
        if (record.auto_attempts < 5) {
          await this.ctx.storage.setAlarm(Date.now() + 60_000);
        } else {
          await this.ctx.storage.deleteAlarm();
          await notifySlack(this.env, draft.slack.channel, draft.slack.ts,
            "⚠️ 자동 승인 발행 요청을 5회 시도했지만 실패했습니다. ✅ 를 눌러 다시 시도할 수 있습니다.");
        }
      }
    });
  }
}

async function autoReaction(env, slack) {
  if (!env.SLACK_BOT_TOKEN) return;
  for (const name of ["robot_face", "white_check_mark"]) {
    try {
      const response = await fetch("https://slack.com/api/reactions.add", {
        method: "POST", headers: { Authorization: `Bearer ${env.SLACK_BOT_TOKEN}`, "Content-Type": "application/json" },
        body: JSON.stringify({ channel: slack.channel, timestamp: slack.ts, name }),
        signal: AbortSignal.timeout(5000),
      });
      const result = await response.json();
      if (!response.ok || (!result.ok && result.error !== "already_reacted")) {
        console.error("auto reaction failed", name, result.error || response.status);
      }
    } catch (err) { console.error("auto reaction failed", name, err.message); }
  }
}

export async function handleImage(request, env, key) {
  if (!env.IMAGES) {
    return new Response("image store not configured", { status: 503 });
  }
  if (request.method === "GET" || request.method === "HEAD") {
    const { value, metadata } = await env.IMAGES.getWithMetadata(key, { type: "arrayBuffer", cacheTtl: 86400 });
    if (value === null) {
      return new Response("not found", { status: 404 });
    }
    return new Response(request.method === "HEAD" ? null : value, {
      headers: {
        "Content-Type": (metadata && metadata.contentType) || "application/octet-stream",
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
      },
    });
  }
  if (request.method !== "PUT") {
    return new Response("method not allowed", { status: 405, headers: { Allow: "GET, HEAD, PUT" } });
  }
  if (!authorized(request, env)) {
    return new Response("unauthorized", { status: 401 });
  }
  const type = (request.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase();
  if (!IMAGE_TYPES.has(type)) {
    return new Response("unsupported image type", { status: 415 });
  }
  const body = await request.arrayBuffer();
  if (body.byteLength === 0 || body.byteLength > MAX_IMAGE_BYTES) {
    return new Response("image too large or empty", { status: 413 });
  }
  await env.IMAGES.put(key, body, { metadata: { contentType: type } });
  const url = `${new URL(request.url).origin}/img/${key}`;
  return new Response(JSON.stringify({ ok: true, url }), {
    status: 201,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

export function parseText(text) {
  const dryRun = new RegExp(DRY_RUN_WORDS.source, "i").test(text);
  const withoutDry = text.replace(DRY_RUN_WORDS, " ");
  const all = new RegExp(ALL_WORDS.source, "i").test(withoutDry);
  const topic = all ? "" : cleanText(withoutDry, MAX_TOPIC);
  return { topic, dryRun, all };
}

export function cleanText(value, limit) {
  return value
    .replace(/[`<>&*_~|!"'\\]/g, "") // 슬랙 mrkdwn·따옴표 제어문자 제거 (멘션·코드블록 탈출 방지)
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, limit);
}

export function buildInputs(mode, topic, dryRun, requestedBy, all = false) {
  // workflow_dispatch 의 inputs 값은 모두 문자열로 전달한다 (boolean 입력도 "true"/"false")
  const inputs = {
    mode,
    topic: mode === "setup" || all ? "" : topic,
    dry_run: dryRun && mode !== "setup" ? "true" : "false",
    source: "slack",
    requested_by: requestedBy,
  };
  // 카테고리별일 때만 보낸다 (워크플로에 없는 입력을 보내면 GitHub 가 거절하므로, 일반 실행은 예전과 같게)
  return all && mode !== "setup" ? { ...inputs, categories: "all" } : inputs;
}

export async function verifySlackSignature(headers, body, secret, nowSeconds = Date.now() / 1000) {
  const timestamp = headers.get("X-Slack-Request-Timestamp");
  const signature = headers.get("X-Slack-Signature");
  if (!secret || !timestamp || !signature) {
    return false;
  }
  if (Math.abs(nowSeconds - Number(timestamp)) > MAX_AGE_SECONDS) {
    return false; // 재전송(replay) 공격 방지
  }
  const expected = await sign(secret, `v0:${timestamp}:${body}`);
  return timingSafeEqual(expected, signature);
}

export async function sign(secret, message) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
  return "v0=" + [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

export function dispatchUrl(env, workflow = env.GITHUB_WORKFLOW || "auto_post.yml") {
  return `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`;
}

async function dispatch(env, inputs, workflow) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
    throw new Error("Worker 에 GITHUB_TOKEN / GITHUB_REPO 가 설정되지 않았습니다");
  }
  const response = await fetch(dispatchUrl(env, workflow), {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "yoopost-slack-relay",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GITHUB_REF || "main", inputs }),
    signal: AbortSignal.timeout(10000),
  });
  if (response.status !== 204 && response.status !== 200) {
    throw new Error(`GitHub API HTTP ${response.status}${await errorDetail(response)}`);
  }
}

// GitHub 오류 본문의 message 만 짧게 전달 (예: "Unexpected inputs provided", "Bad credentials")
async function errorDetail(response) {
  try {
    const body = await response.json();
    const message = cleanText(String(body.message || ""), 80);
    return message ? ` — ${message}` : "";
  } catch {
    return "";
  }
}

function reply(text, responseType = "ephemeral") {
  return new Response(JSON.stringify({ response_type: responseType, text }), {
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}
