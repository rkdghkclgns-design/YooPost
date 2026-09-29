/**
 * 슬랙(YooPost 워크스페이스) 슬래시 커맨드 → GitHub Actions(workflow_dispatch) 중계 Cloudflare Worker.
 *
 * GitHub Actions 는 외부 요청(웹훅)을 직접 받을 수 없어서, 슬랙 명령을 이 Worker 가 받아
 * 서명 검증 후 GitHub API 로 워크플로를 실행시킨다. (Cloudflare 무료 플랜으로 충분)
 *
 * 필요한 설정 (wrangler secret put / wrangler.toml [vars])
 *   SLACK_SIGNING_SECRET : 슬랙 앱 > Basic Information > Signing Secret
 *   GITHUB_TOKEN         : fine-grained PAT (YooPost 리포지토리 Actions: Read and write 만)
 *   GITHUB_REPO          : "깃허브아이디/YooPost"
 *   GITHUB_WORKFLOW      : (선택) 워크플로 파일명, 기본 auto_post.yml
 *   GITHUB_REF           : (선택) 실행 브랜치, 기본 main
 *   ALLOWED_USER_IDS     : (선택) 실행 허용 슬랙 사용자 ID 목록 "U0123,U0456"
 *   IMAGE_UPLOAD_TOKEN   : 블로그 이미지 업로드용 비밀값 (GitHub Actions 와 같은 값)
 *   IMAGES               : 이미지 저장 KV 바인딩 (wrangler.toml [[kv_namespaces]])
 *
 * 사용 예:  /aipost                       → 최신 AI 소식에서 주제를 골라 포스팅
 *           /aipost Gemini 3.8 Live       → 주제 지정
 *           /aipost 테스트                → 발행 없이 조사·원고·사실 검증 미리보기(dry-run)
 *           /aipost 전체                  → 블로그 메뉴 카테고리마다 1편씩 (7편, 차례로)
 *           /aipost 도움말                → 사용법 안내 (실행 안 함)
 *           /aipost-setup                 → 연동 상태 점검
 *
 * 블로그 이미지 호스팅 (구글 드라이브 대신):
 *   PUT /img/<이름>  (Authorization: Bearer IMAGE_UPLOAD_TOKEN) → KV 에 저장
 *   GET /img/<이름>  → 누구나 볼 수 있는 이미지 주소 (구글 블로그 본문 <img> 에 사용)
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

export const HELP_TEXT = [
  "*🤖 AI백과사전 자동 포스팅 명령어*",
  "• `/aipost` — 최신 AI 공식 발표·뉴스에서 주제를 골라 바로 포스팅",
  "• `/aipost Gemini 3.8 Live` — 주제 지정 (제품·모델·논문 이름)",
  "• `/aipost 테스트` — 발행 없이 자료 조사·원고·사실 검증·미리보기만 (`/aipost GPT-6 테스트` 처럼 주제와 함께 가능)",
  "• `/aipost 전체` — 블로그 메뉴 카테고리(AI 언어 모델·AI 도구·AI 논문 분석·AI 뉴스 리뷰·AI 보안·AI 인사이트·AI 공모주 분석)마다 1편씩, 차례로 7편 (`/aipost 전체 테스트` 가능)",
  "• `/aipost-setup` — 구글 블로그·Gemini·이미지 저장소 연동 상태 점검",
  "",
  "실행하면 채널에 진행 메시지가 올라오고 단계별(주제 선정 → 자료 조사 → 원고 → 사실 검증 → 이미지 → 발행)로 갱신됩니다.",
  "사실 검증을 통과하지 못한 글은 발행하지 않고 원고·검증 보고만 남깁니다.",
].join("\n");

export default {
  async fetch(request, env) {
    const image = new URL(request.url).pathname.match(IMAGE_PATH);
    if (image) {
      return handleImage(request, env, image[1]);
    }
    if (request.method !== "POST") {
      return new Response("ok");
    }
    const raw = await request.text();
    if (!(await verifySlackSignature(request.headers, raw, env.SLACK_SIGNING_SECRET))) {
      return new Response("invalid signature", { status: 401 });
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
  const expected = env.IMAGE_UPLOAD_TOKEN ? `Bearer ${env.IMAGE_UPLOAD_TOKEN}` : "";
  if (!expected || !timingSafeEqual(expected, request.headers.get("Authorization") || "")) {
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

export function dispatchUrl(env) {
  const workflow = env.GITHUB_WORKFLOW || "auto_post.yml";
  return `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`;
}

async function dispatch(env, inputs) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
    throw new Error("Worker 에 GITHUB_TOKEN / GITHUB_REPO 가 설정되지 않았습니다");
  }
  const response = await fetch(dispatchUrl(env), {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "yoopost-slack-relay",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GITHUB_REF || "main", inputs }),
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
