import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { CloudWatchdog } from "./watchdog.js";
import worker from "./worker.js";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

function fixture(t, { runs = [], jobs = [], deployments = [], racing = false, failing = false } = {}) {
  let now = Date.parse("2026-10-07T00:50:00Z");
  t.mock.method(Date, "now", () => now);
  const data = new Map(), calls = [];
  let active = runs;
  const env = { GITHUB_REPO: "test/YooPost", GITHUB_TOKEN: "test", GITHUB_REF: "main",
    CODEX_FALLBACK_ENABLED: "true", SLACK_BOT_TOKEN: "test", WATCHDOG_CHANNEL_ID: "CALERT" };
  const ctx = { storage: {
    async get(key) { return structuredClone(data.get(key)); },
    async put(key, value) { data.set(key, structuredClone(value)); },
  }, async blockConcurrencyWhile(fn) { return fn(); } };
  globalThis.fetch = async (url, init = {}) => {
    const u = new URL(url);
    calls.push({ path: u.pathname, method: init.method || "GET", body: init.body && JSON.parse(init.body) });
    if (u.hostname === "slack.com") return Response.json({ ok: true });
    if (failing) return Response.json({ message: "Bad credentials" }, { status: 401 });
    if (u.pathname.endsWith("/jobs")) return Response.json({ total_count: jobs.length,
      jobs: racing && calls.filter(c => c.path.endsWith("/jobs")).length > 1
        ? [{ status: "in_progress", created_at: old, runner_id: 1 }] : jobs });
    if (u.pathname.endsWith("/pending_deployments")) return Response.json(deployments);
    if (u.pathname.endsWith("/cancel")) { active = []; return new Response(null, { status: 202 }); }
    if (u.pathname.endsWith("/dispatches")) return new Response(null, { status: 204 });
    if (u.pathname.endsWith("/runs")) {
      if (u.pathname.includes("cloud_drafts.yml")) return Response.json({ total_count: 0, workflow_runs: [] });
      const status = u.searchParams.get("status");
      const found = status ? active.filter(r => r.status === status) : active;
      return Response.json({ total_count: found.length, workflow_runs: found });
    }
    return Response.json(active[0] || { ...runs[0], status: "completed" });
  };
  let obj = new CloudWatchdog(ctx, env);
  return { env, ctx, calls, data, advance(ms) { now += ms; },
    restart() { obj = new CloudWatchdog(ctx, env); },
    async tick() { return (await obj.fetch(new Request("https://test/watchdog", { method: "POST" }))).json(); },
  };
}
const old = "2026-10-06T08:49:00Z";
const queued = { id: 1, status: "queued", created_at: old, html_url: "https://github.com/test/YooPost/actions/runs/1" };
const waitingJobs = [{ name: "write", status: "queued", created_at: old, runner_id: null }];

test("stale queue cancels once, waits for cancellation, then dispatches after object restart", async t => {
  const f = fixture(t, { runs: [queued], jobs: waitingJobs });
  await f.tick();
  assert.equal(f.calls.filter(c => c.path.endsWith("/cancel")).length, 1);
  assert.equal(f.calls.filter(c => c.path.includes("codex_fallback.yml/dispatches")).length, 0);
  f.restart();
  f.advance(5 * 60_000);
  await f.tick();
  const dispatches = f.calls.filter(c => c.path.includes("codex_fallback.yml/dispatches"));
  assert.equal(dispatches.length, 1);
  assert.deepEqual(dispatches[0].body, { ref: "main", inputs: { mode: "work" } });
  await f.tick();
  assert.equal(f.calls.filter(c => c.path.includes("codex_fallback.yml/dispatches")).length, 1);
});

test("active writer protects both itself and its queued follow-up", async t => {
  const f = fixture(t, { runs: [{ ...queued, id: 2, status: "in_progress" }, queued],
    jobs: [{ name: "write", status: "in_progress", runner_id: 1, created_at: old }] });
  const state = await f.tick();
  assert.equal(state.workflows["codex_fallback.yml"].status, "running");
  assert.equal(f.calls.filter(c => c.path.endsWith("/cancel")).length, 0);
});

test("recent queue and approval protection are never cancelled", async t => {
  for (const options of [
    { runs: [{ ...queued, created_at: "2026-10-07T00:49:00Z" }], jobs: [] },
    { runs: [queued], jobs: waitingJobs, deployments: [{ reviewers: [{ type: "User" }] }] },
    { runs: [queued], jobs: waitingJobs, deployments: [{ wait_timer: 30 }] },
  ]) {
    const f = fixture(t, options);
    await f.tick();
    assert.equal(f.calls.filter(c => c.path.endsWith("/cancel")).length, 0);
  }
});

test("server assigned between status checks prevents cancellation", async t => {
  const f = fixture(t, { runs: [queued], jobs: waitingJobs, racing: true });
  await f.tick();
  assert.equal(f.calls.filter(c => c.path.endsWith("/cancel")).length, 0);
});

test("finishing a long write gives the result-saving job time to appear", async t => {
  const f = fixture(t, { runs: [{ ...queued, status: "in_progress" }], jobs: [
    { name: "write", status: "completed", completed_at: "2026-10-07T00:49:00Z", runner_id: 1 },
  ] });
  await f.tick();
  assert.equal(f.calls.filter(c => c.path.endsWith("/cancel")).length, 0);
});

test("missed schedule is dispatched independently and limited across restarts", async t => {
  const f = fixture(t);
  await f.tick();
  f.restart();
  f.advance(5 * 60_000);
  await f.tick();
  assert.equal(f.calls.filter(c => c.path.includes("codex_fallback.yml/dispatches")).length, 1);
  for (let i = 0; i < 8; i++) { f.advance(30 * 60_000); await f.tick(); }
  const state = f.data.get("state");
  assert.ok(state.workflows["codex_fallback.yml"].actions.length <= 6);
});

test("retry storm backs off and authentication errors notify only once", async t => {
  const f = fixture(t);
  const now = Date.now();
  f.data.set("state", { workflows: { "codex_fallback.yml": { recovering: true, actions: Array(6).fill(now) } } });
  await f.tick();
  await f.tick();
  assert.equal(f.calls.filter(c => c.path.includes("codex_fallback.yml/dispatches")).length, 0);
  assert.equal(f.calls.filter(c => c.path.endsWith("chat.postMessage")).length, 1);
  const g = fixture(t, { failing: true });
  assert.equal((await g.tick()).status, "error");
  g.restart();
  await g.tick();
  assert.equal(g.calls.filter(c => c.path.endsWith("chat.postMessage")).length, 1);
});

test("watchdog endpoint requires the existing private storage credential", async () => {
  const response = await worker.fetch(new Request("https://test/watchdog", { method: "POST" }), {
    IMAGE_UPLOAD_TOKEN: "private", CLOUD_WATCHDOG: { get() { throw new Error("must not run"); } },
  });
  assert.equal(response.status, 401);
});

test("a transient GitHub read timeout retries without duplicate dispatches", async t => {
  const f = fixture(t);
  const fetch = globalThis.fetch;
  let reads = 0;
  globalThis.fetch = async (url, init) => {
    const u = new URL(url);
    if (u.pathname.includes("codex_fallback.yml/runs") && u.searchParams.get("status") === "queued") {
      reads++;
      if (reads === 1) throw new DOMException("read timed out", "TimeoutError");
    }
    return fetch(url, init);
  };
  const state = await f.tick();
  assert.equal(state.status, "ok");
  assert.equal(reads, 2);
  for (const workflow of ["codex_fallback.yml", "cloud_drafts.yml"]) {
    assert.equal(f.calls.filter(c => c.path.includes(workflow + "/dispatches")).length, 1);
    assert.equal(state.workflows[workflow].actions.length, 1);
  }
});

test("persistent GitHub read timeouts stop after one retry and identify the endpoint", async t => {
  const f = fixture(t);
  const fetch = globalThis.fetch;
  let reads = 0;
  globalThis.fetch = async (url, init) => {
    const u = new URL(url);
    if (u.pathname.includes("codex_fallback.yml/runs") && u.searchParams.get("status") === "queued") {
      reads++;
      throw new DOMException("read timed out", "TimeoutError");
    }
    return fetch(url, init);
  };
  const state = await f.tick();
  assert.equal(state.status, "error");
  assert.equal(reads, 2);
  assert.match(state.error, /GET actions\/workflows\/codex_fallback.yml\/runs/);
  assert.equal(f.calls.filter(c => c.path.endsWith("/dispatches")).length, 0);
});

test("an ambiguous dispatch timeout is not retried after restarting the watchdog", async t => {
  const f = fixture(t);
  const fetch = globalThis.fetch;
  let dispatches = 0;
  globalThis.fetch = async (url, init) => {
    if (new URL(url).pathname.includes("codex_fallback.yml/dispatches")) {
      dispatches++;
      throw new DOMException("dispatch response timed out", "TimeoutError");
    }
    return fetch(url, init);
  };
  const state = await f.tick();
  assert.equal(state.status, "error");
  assert.equal(state.workflows["codex_fallback.yml"].actions.length, 1);
  f.restart();
  f.advance(60_000);
  await f.tick();
  assert.equal(dispatches, 1);
});

test("read retry cannot extend the watchdog's shared time budget", async t => {
  const f = fixture(t);
  const obj = new CloudWatchdog(f.ctx, f.env);
  obj.deadline = Date.now() + 20_000;
  let reads = 0;
  globalThis.fetch = async () => {
    reads++;
    f.advance(20_001);
    throw new DOMException("read timed out", "TimeoutError");
  };
  await assert.rejects(obj.api("actions/workflows/codex_fallback.yml/runs"), /시간 초과/);
  assert.equal(reads, 1);
});

test("endpoint diagnostics preserve the existing timeout notice deduplication", async t => {
  const f = fixture(t);
  const notice = "⚠️ YooPost 클라우드 복구 점검 실패: The operation was aborted due to timeout";
  f.data.set("state", { workflows: {}, notices: { [notice]: Date.now() } });
  const fetch = globalThis.fetch;
  let method = "GET";
  globalThis.fetch = async (url, init) => {
    const u = new URL(url);
    if ((method === "GET" && u.pathname.includes("codex_fallback.yml/runs") && u.searchParams.get("status") === "queued") ||
        (method === "POST" && u.pathname.includes("codex_fallback.yml/dispatches"))) {
      throw new DOMException("operation timed out", "TimeoutError");
    }
    return fetch(url, init);
  };
  assert.match((await f.tick()).error, /GET actions\/workflows/);
  method = "POST";
  assert.match((await f.tick()).error, /POST actions\/workflows/);
  assert.equal(f.calls.filter(c => c.path.endsWith("chat.postMessage")).length, 0);
});
