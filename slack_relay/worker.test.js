// node --test  (Node 18+ : 전역 crypto.subtle / fetch / Request / Response 사용)
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";

import worker, { buildInputs, dispatchUrl, parseText, sign, verifySlackSignature } from "./worker.js";

const SECRET = "test-signing-secret";
const ENV = { SLACK_SIGNING_SECRET: SECRET, GITHUB_TOKEN: "ghp_x", GITHUB_REPO: "me/YooPost" };
const DISPATCH_URL = "https://api.github.com/repos/me/YooPost/actions/workflows/auto_post.yml/dispatches";
const realFetch = globalThis.fetch;
let dispatched;

beforeEach(() => {
  dispatched = [];
  globalThis.fetch = async (url, init) => {
    dispatched.push({ url, init, body: JSON.parse(init.body) });
    return new Response(null, { status: 204 });
  };
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

async function slackRequest(params, { secret = SECRET, timestamp = Math.floor(Date.now() / 1000) } = {}) {
  const body = new URLSearchParams(params).toString();
  const signature = await sign(secret, `v0:${timestamp}:${body}`);
  return new Request("https://relay.example/", {
    method: "POST",
    headers: { "X-Slack-Request-Timestamp": String(timestamp), "X-Slack-Signature": signature },
    body,
  });
}

test("parseText extracts topic and dry-run flag", () => {
  assert.deepEqual(parseText("Gemini 3.8 Live 테스트"), { topic: "Gemini 3.8 Live", dryRun: true, all: false });
  assert.deepEqual(parseText("  "), { topic: "", dryRun: false, all: false });
  assert.deepEqual(parseText("DRY-RUN GPT-6"), { topic: "GPT-6", dryRun: true, all: false });
  assert.equal(parseText("x".repeat(150)).topic.length, 100);
});

test("parseText recognises the all-categories words", () => {
  assert.deepEqual(parseText("전체"), { topic: "", dryRun: false, all: true });
  assert.deepEqual(parseText("카테고리별 테스트"), { topic: "", dryRun: true, all: true });
  assert.deepEqual(parseText("ALL"), { topic: "", dryRun: false, all: true });
  assert.equal(parseText("전체적인 AI 흐름").all, false); // 단어 일부는 아님
});

test("parseText strips slack mrkdwn control characters and quotes", () => {
  assert.deepEqual(parseText("```<!channel> GPT"), { topic: "channel GPT", dryRun: false, all: false });
  assert.deepEqual(parseText(`"Claude"'5'\\`), { topic: "Claude5", dryRun: false, all: false });
});

test("buildInputs sends string inputs and ignores topic/dry-run for setup", () => {
  assert.deepEqual(buildInputs("post", "GPT-6", true, "yoo"), {
    mode: "post", topic: "GPT-6", dry_run: "true", source: "slack", requested_by: "yoo",
  });
  assert.deepEqual(buildInputs("setup", "GPT-6", true, "yoo"), {
    mode: "setup", topic: "", dry_run: "false", source: "slack", requested_by: "yoo",
  });
  assert.deepEqual(buildInputs("post", "", true, "yoo", true), {
    mode: "post", topic: "", dry_run: "true", source: "slack", requested_by: "yoo", categories: "all",
  });
  assert.equal(buildInputs("setup", "", false, "yoo", true).categories, undefined);
});

test("/aipost no longer runs Gemini — it explains the Claude + ✅ flow", async () => {
  for (const text of ["", "GPT-6 테스트", "전체"]) {
    const response = await worker.fetch(await slackRequest({ command: "/aipost", text, user_id: "U1" }), ENV);
    const json = await response.json();
    assert.equal(json.response_type, "ephemeral");
    assert.match(json.text, /Claude 앱/);
  }
  assert.equal(dispatched.length, 0);
});

test("dispatchUrl honours custom workflow file", () => {
  assert.equal(dispatchUrl(ENV), DISPATCH_URL);
  assert.equal(
    dispatchUrl({ ...ENV, GITHUB_WORKFLOW: "other.yml" }),
    "https://api.github.com/repos/me/YooPost/actions/workflows/other.yml/dispatches",
  );
});

test("signature verification rejects tampering and stale requests", async () => {
  const ts = "1700000000";
  const good = await sign(SECRET, `v0:${ts}:a=1`);
  const headers = (sig, t = ts) => new Headers({ "X-Slack-Request-Timestamp": t, "X-Slack-Signature": sig });
  assert.equal(await verifySlackSignature(headers(good), "a=1", SECRET, 1700000000), true);
  assert.equal(await verifySlackSignature(headers(good), "a=2", SECRET, 1700000000), false);
  assert.equal(await verifySlackSignature(headers(good), "a=1", SECRET, 1700000000 + 600), false);
  assert.equal(await verifySlackSignature(headers(good), "a=1", "", 1700000000), false);
});

test("/aipost-setup dispatches the workflow with inputs", async () => {
  const request = await slackRequest({ command: "/aipost-setup", user_id: "U1", user_name: "yoo" });
  const response = await worker.fetch(request, ENV);
  const json = await response.json();
  assert.equal(json.response_type, "in_channel");
  assert.match(json.text, /연동 상태 점검/);
  assert.equal(dispatched[0].url, DISPATCH_URL);
  assert.match(dispatched[0].init.headers.Authorization, /^Bearer ghp_x$/);
  assert.deepEqual(dispatched[0].body, {
    ref: "main",
    inputs: { mode: "setup", topic: "", dry_run: "false", source: "slack", requested_by: "yoo" },
  });
});

test("custom ref is used when configured", async () => {
  await worker.fetch(await slackRequest({ command: "/aipost-setup", user_id: "U1" }), { ...ENV, GITHUB_REF: "dev" });
  assert.equal(dispatched[0].body.ref, "dev");
});

test("setup commands map to setup mode", async () => {
  for (const command of ["/aipost-setup", "/초기세팅"]) {
    const response = await worker.fetch(await slackRequest({ command, user_id: "U1" }), ENV);
    assert.equal(response.status, 200);
  }
  assert.deepEqual(dispatched.map((d) => d.body.inputs.mode), ["setup", "setup"]);
});

test("help text is returned without dispatching", async () => {
  for (const text of ["도움말", "help", "?"]) {
    const response = await worker.fetch(await slackRequest({ command: "/aipost", text, user_id: "U1" }), ENV);
    const json = await response.json();
    assert.equal(json.response_type, "ephemeral");
    assert.match(json.text, /사용법/);
  }
  assert.equal(dispatched.length, 0);
});

test("bad signature returns 401 and does not dispatch", async () => {
  const response = await worker.fetch(await slackRequest({ command: "/aipost" }, { secret: "wrong" }), ENV);
  assert.equal(response.status, 401);
  assert.equal(dispatched.length, 0);
});

test("user allowlist is enforced", async () => {
  const env = { ...ENV, ALLOWED_USER_IDS: "U9, U8" };
  const response = await worker.fetch(await slackRequest({ command: "/aipost-setup", user_id: "U1" }), env);
  assert.match((await response.json()).text, /권한/);
  assert.equal(dispatched.length, 0);
});

test("unknown command, missing config and GitHub failure are reported ephemerally", async () => {
  const unknown = await worker.fetch(await slackRequest({ command: "/hack" }), ENV);
  assert.match((await unknown.json()).text, /알 수 없는 명령어/);

  const noToken = await worker.fetch(await slackRequest({ command: "/aipost-setup" }), { ...ENV, GITHUB_TOKEN: "" });
  assert.match((await noToken.json()).text, /GITHUB_TOKEN/);

  globalThis.fetch = async () => new Response("nope", { status: 404 });
  const failed = await worker.fetch(await slackRequest({ command: "/aipost-setup" }), ENV);
  const json = await failed.json();
  assert.equal(json.response_type, "ephemeral");
  assert.match(json.text, /HTTP 404$/);
});

test("GitHub error message is included without markup", async () => {
  globalThis.fetch = async () => new Response(
    JSON.stringify({ message: 'Unexpected inputs provided: ["<!channel>"]' }), { status: 422 },
  );
  const response = await worker.fetch(await slackRequest({ command: "/aipost-setup" }), ENV);
  const { text } = await response.json();
  assert.match(text, /HTTP 422 — Unexpected inputs provided: \[channel\]/);
});

test("GET health check", async () => {
  const response = await worker.fetch(new Request("https://relay.example/"), ENV);
  assert.equal(await response.text(), "ok");
});

// ---------------------------------------------------------------- 블로그 이미지 호스팅 (KV)
function memoryKv() {
  const store = new Map();
  return {
    store,
    async put(key, value, options) {
      store.set(key, { value, metadata: options && options.metadata });
    },
    async getWithMetadata(key) {
      const item = store.get(key);
      return item ? { value: item.value, metadata: item.metadata } : { value: null, metadata: null };
    },
  };
}

const PNG = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);

function upload(key, { token = "img-secret", type = "image/png", body = PNG } = {}) {
  return new Request(`https://relay.example/img/${key}`, {
    method: "PUT",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": type },
    body,
  });
}

test("image upload with the token stores it and GET serves it publicly", async () => {
  const env = { ...ENV, IMAGES: memoryKv(), IMAGE_UPLOAD_TOKEN: "img-secret" };
  const put = await worker.fetch(upload("20260929_1000_cover.png"), env);
  assert.equal(put.status, 201);
  assert.deepEqual(await put.json(), { ok: true, url: "https://relay.example/img/20260929_1000_cover.png" });

  const got = await worker.fetch(new Request("https://relay.example/img/20260929_1000_cover.png"), env);
  assert.equal(got.status, 200);
  assert.equal(got.headers.get("Content-Type"), "image/png");
  assert.match(got.headers.get("Cache-Control"), /immutable/);
  assert.deepEqual(new Uint8Array(await got.arrayBuffer()), PNG);
  assert.equal(dispatched.length, 0);
});

test("image upload is refused without the right token, type or size", async () => {
  const env = { ...ENV, IMAGES: memoryKv(), IMAGE_UPLOAD_TOKEN: "img-secret" };
  assert.equal((await worker.fetch(upload("a.png", { token: "wrong" }), env)).status, 401);
  assert.equal((await worker.fetch(upload("a.png"), { ...env, IMAGE_UPLOAD_TOKEN: "" })).status, 401);
  assert.equal((await worker.fetch(upload("a.svg", { type: "image/svg+xml" }), env)).status, 415);
  assert.equal((await worker.fetch(upload("a.png", { body: new Uint8Array(0) }), env)).status, 413);
  assert.equal(env.IMAGES.store.size, 0);
});

test("image routes: missing image, bad method, unsafe names and missing store", async () => {
  const env = { ...ENV, IMAGES: memoryKv(), IMAGE_UPLOAD_TOKEN: "img-secret" };
  assert.equal((await worker.fetch(new Request("https://relay.example/img/nope.png"), env)).status, 404);
  const del = await worker.fetch(new Request("https://relay.example/img/a.png", { method: "DELETE" }), env);
  assert.equal(del.status, 405);
  // 경로 조작 이름은 이미지 경로로 인정하지 않는다 → 일반 GET 응답
  const traversal = await worker.fetch(new Request("https://relay.example/img/..%2Fsecret"), env);
  assert.equal(await traversal.text(), "ok");
  const noStore = await worker.fetch(new Request("https://relay.example/img/a.png"), ENV);
  assert.equal(noStore.status, 503);
});


// ---------------------------------------------------------------- 원고 보관함 + 슬랙 ✅ 승인
function memoryStore() {
  const store = new Map();
  return {
    store,
    async put(key, value) {
      store.set(key, value);
    },
    async get(key, options) {
      const value = store.has(key) ? store.get(key) : null;
      return value !== null && options && options.type === "json" ? JSON.parse(value) : value;
    },
  };
}

function draftRequest(id, body, { method = "PUT", token = "img-secret" } = {}) {
  return new Request(`https://relay.example/draft/${id}`, {
    method,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: method === "PUT" ? JSON.stringify(body) : undefined,
  });
}

async function eventRequest(payload, { retry = false } = {}) {
  const body = JSON.stringify(payload);
  const timestamp = Math.floor(Date.now() / 1000);
  const headers = { "X-Slack-Request-Timestamp": String(timestamp), "X-Slack-Signature": await sign(SECRET, `v0:${timestamp}:${body}`),
    "Content-Type": "application/json" };
  if (retry) headers["X-Slack-Retry-Num"] = "1";
  return new Request("https://relay.example/slack/events", { method: "POST", headers, body });
}

function reaction(reactionName = "white_check_mark", user = "U1", ts = "1700.0001") {
  return { type: "event_callback", event: { type: "reaction_added", user, reaction: reactionName,
    item: { type: "message", channel: "CDRAFT", ts } } };
}

const DRAFT = { title: "제목", html: "<p>본문</p>", labels: ["AI 도구"], status: "pending",
  slack: { channel: "CDRAFT", ts: "1700.0001" } };

function draftEnv(extra = {}) {
  return { ...ENV, IMAGES: memoryStore(), IMAGE_UPLOAD_TOKEN: "img-secret", ...extra };
}

test("drafts are stored with a token, linked to their Slack message and readable", async () => {
  const env = draftEnv();
  const put = await worker.fetch(draftRequest("20260929_1300_1", DRAFT), env);
  assert.equal(put.status, 201);
  assert.equal(env.IMAGES.store.get("msg:CDRAFT:1700.0001"), "20260929_1300_1");
  const got = await worker.fetch(draftRequest("20260929_1300_1", null, { method: "GET" }), env);
  assert.equal((await got.json()).title, "제목");
  assert.equal((await worker.fetch(draftRequest("x", DRAFT, { token: "wrong" }), env)).status, 401);
  assert.equal((await worker.fetch(draftRequest("x", { title: "only" }), env)).status, 400);
  assert.equal((await worker.fetch(draftRequest("nope", null, { method: "GET" }), env)).status, 404);
  const del = new Request("https://relay.example/draft/x", { method: "DELETE", headers: { Authorization: "Bearer img-secret" } });
  assert.equal((await worker.fetch(del, env)).status, 405);
  const bad = new Request("https://relay.example/draft/x", { method: "PUT", headers: { Authorization: "Bearer img-secret" }, body: "{oops" });
  assert.equal((await worker.fetch(bad, env)).status, 400);
  assert.equal((await worker.fetch(draftRequest("x", DRAFT), ENV)).status, 503);
});

test("slack event URL verification returns the challenge", async () => {
  const response = await worker.fetch(await eventRequest({ type: "url_verification", challenge: "abc" }), draftEnv());
  assert.deepEqual(await response.json(), { challenge: "abc" });
});

test("✅ on a draft message dispatches publishing once", async () => {
  const env = draftEnv();
  await worker.fetch(draftRequest("d1", DRAFT), env);
  const response = await worker.fetch(await eventRequest(reaction()), env);
  assert.equal(response.status, 200);
  assert.equal(dispatched.length, 1);
  assert.equal(dispatched[0].url, "https://api.github.com/repos/me/YooPost/actions/workflows/publish_draft.yml/dispatches");
  assert.deepEqual(dispatched[0].body.inputs, { draft_id: "d1", approved_by: "U1" });
  assert.equal(JSON.parse(env.IMAGES.store.get("draft:d1")).status, "approved");
  await worker.fetch(await eventRequest(reaction()), env); // 두 번째 ✅ 는 무시
  await worker.fetch(await eventRequest(reaction(), { retry: true }), env); // 슬랙 재전송 무시
  assert.equal(dispatched.length, 1);
});

test("other reactions, other messages, disallowed users and bad payloads are ignored", async () => {
  const env = draftEnv({ ALLOWED_USER_IDS: "U9" });
  await worker.fetch(draftRequest("d1", DRAFT), env);
  await worker.fetch(await eventRequest(reaction("eyes")), env);
  await worker.fetch(await eventRequest(reaction("white_check_mark", "U1", "9999.1")), env);
  await worker.fetch(await eventRequest(reaction("white_check_mark", "U1")), env);
  assert.equal(dispatched.length, 0);
  await worker.fetch(await eventRequest(reaction("heavy_check_mark::skin-tone-2", "U9")), env);
  assert.equal(dispatched.length, 1);
  const notJson = new Request("https://relay.example/slack/events", { method: "POST", body: "x",
    headers: { "X-Slack-Request-Timestamp": String(Math.floor(Date.now() / 1000)),
      "X-Slack-Signature": await sign(SECRET, `v0:${Math.floor(Date.now() / 1000)}:x`) } });
  assert.equal((await worker.fetch(notJson, env)).status, 400);
});

test("failed dispatch puts the draft back to pending so ✅ can retry", async () => {
  const env = draftEnv();
  await worker.fetch(draftRequest("d1", DRAFT), env);
  globalThis.fetch = async () => new Response("nope", { status: 500 });
  const response = await worker.fetch(await eventRequest(reaction()), env);
  assert.equal(response.status, 200);
  assert.equal(JSON.parse(env.IMAGES.store.get("draft:d1")).status, "pending");
});

test("approval waits in the background when the runtime offers waitUntil", async () => {
  const env = draftEnv();
  await worker.fetch(draftRequest("d1", DRAFT), env);
  const pending = [];
  await worker.fetch(await eventRequest(reaction()), env, { waitUntil: (p) => pending.push(p) });
  await Promise.all(pending);
  assert.equal(dispatched.length, 1);
});
