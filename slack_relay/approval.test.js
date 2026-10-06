import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import worker, { DraftApproval, approveDraft, enqueueManual, handleSlackEvent } from "./worker.js";

const realFetch = globalThis.fetch;
let calls;
beforeEach(() => {
  calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, body: init.body ? JSON.parse(init.body) : undefined });
    if (String(url).includes("/conversations.history?")) {
      return Response.json({ ok: true, messages: [{ ts: "1791244200.000001", text: "검증된 원고",
        blocks: [{ type: "section", block_id: "draft_title", text: { type: "mrkdwn", text: "검증된 원고" } }] }] });
    }
    return String(url).startsWith("https://slack.com/")
      ? Response.json({ ok: true }) : new Response(null, { status: 204 });
  };
});
afterEach(() => { globalThis.fetch = realFetch; });

function fixture(t) {
  let now = 1_791_244_200_000;
  t.mock.method(Date, "now", () => now);
  const kv = new Map();
  const env = { IMAGE_UPLOAD_TOKEN: "test", GITHUB_TOKEN: "test", GITHUB_REPO: "me/YooPost",
    AUTO_APPROVE_ENABLED: "true", CODEX_FALLBACK_ENABLED: "true", ALLOWED_USER_IDS: "U1", SLACK_BOT_TOKEN: "test",
    IMAGES: {
      async put(key, value) { kv.set(key, value); },
      async get(key, options) {
        const value = kv.get(key) ?? null;
        return value !== null && options?.type === "json" ? JSON.parse(value) : value;
      },
    } };
  const instances = new Map();
  env.DRAFT_APPROVAL = {
    idFromName(id) { return id; },
    get(id) {
      if (!instances.has(id)) {
        const data = new Map();
        let queue = Promise.resolve();
        const ctx = { alarmAt: null, storage: {
          async get(key) { return structuredClone(data.get(key)); },
          async put(key, value) { data.set(key, structuredClone(value)); },
          async setAlarm(at) { ctx.alarmAt = at; },
          async deleteAlarm() { ctx.alarmAt = null; },
          async transaction(fn) {
            const snapshot = structuredClone(data), at = ctx.alarmAt;
            try { return await fn(ctx.storage); }
            catch (err) { data.clear(); for (const [k, v] of snapshot) data.set(k, v); ctx.alarmAt = at; throw err; }
          },
        }, blockConcurrencyWhile(fn) {
          const result = queue.then(fn);
          queue = result.catch(() => {});
          return result;
        } };
        instances.set(id, { ctx, obj: new DraftApproval(ctx, env) });
      }
      return instances.get(id).obj;
    },
  };
  const draft = { title: "검증된 원고", html: "<p>내용</p>", status: "pending",
    slack: { channel: "CDRAFT", ts: "1791244200.000001" } };
  return { env, draft, kv, instances,
    advance(ms) { now += ms; },
    async save(data = draft, id = "d1") {
      return worker.fetch(new Request(`https://test/draft/${id}`, { method: "PUT",
        headers: { Authorization: "Bearer test" }, body: JSON.stringify(data) }), env);
    },
    async read() {
      return (await worker.fetch(new Request("https://test/draft/d1", {
        headers: { Authorization: "Bearer test" },
      }), env)).json();
    },
    alarm() { return env.DRAFT_APPROVAL.get("d1").alarm(); },
    manual(reaction = "white_check_mark", user = "U1") {
      return approveDraft(env, { reaction, user, item: { type: "message", channel: "CDRAFT", ts: draft.slack.ts } });
    },
  };
}

function publishes() { return calls.filter(c => c.url.includes("/publish_draft.yml/dispatches")); }

test("60 seconds of pending approval requests publishing once, also after object restart", async t => {
  const f = fixture(t);
  assert.equal((await f.save()).status, 201);
  assert.equal(Date.parse((await f.read()).auto_approve_at), Date.now() + 60_000);
  f.advance(59_999);
  await f.alarm();
  assert.equal(publishes().length, 0);
  // PC/Worker 프로세스가 살아 있어야 하는 타이머가 아니다. 영속 저장을 새 객체가 읽는다.
  const item = f.instances.get("d1");
  item.obj = new DraftApproval(item.ctx, f.env);
  f.advance(1);
  await f.alarm();
  await f.alarm();
  assert.equal(publishes().length, 1);
  assert.deepEqual(publishes()[0].body.inputs, { draft_id: "d1", approved_by: "auto-60s" });
  assert.equal((await f.read()).status, "approved");
  assert.equal(item.ctx.alarmAt, Date.now() + 15 * 60_000);
  const markers = calls.filter(c => c.url.endsWith("/chat.update"));
  assert.equal(markers.length, 1);
  assert.equal(markers[0].body.blocks[0].block_id, "draft_title");
  assert.equal(markers[0].body.blocks.at(-1).elements[0].text, "🤖✅ 자동 승인 (1분 경과)");
});

test("manual approval wins before the timeout; stale writes cannot rearm it", async t => {
  const f = fixture(t);
  await f.save();
  f.advance(20_000);
  assert.equal(await f.manual(), "dispatched");
  await f.save(); // 승인 요청 마지막 PUT 이 뒤늦게 도착한 상황
  f.advance(40_000);
  await f.alarm();
  assert.equal(publishes().length, 1);
  assert.equal((await f.read()).approved_by, "U1");
  assert.equal(calls.filter(c => c.url.endsWith("/chat.update")).length, 0);
});

test("simultaneous manual and timer approval do not dispatch twice", async t => {
  const f = fixture(t);
  await f.save();
  f.advance(60_000);
  await Promise.all([f.alarm(), f.manual(), f.manual()]);
  assert.equal(publishes().length, 1);
});

test("❌ by the owner cancels auto approval; an explicit ✅ can resume it", async t => {
  const f = fixture(t);
  await f.save();
  assert.equal(await f.manual("x", "U2"), "not allowed");
  assert.equal(await f.manual("x"), "held");
  await f.save();
  f.advance(60_000);
  await f.alarm();
  assert.equal(publishes().length, 0);
  assert.equal((await f.read()).status, "held");
  assert.equal(await f.manual(), "dispatched");
  assert.equal(publishes().length, 1);
});

test("saving again does not postpone the original deadline; no Slack link means no timer", async t => {
  const f = fixture(t);
  await f.save({ ...f.draft, slack: undefined });
  assert.equal(f.instances.get("d1").ctx.alarmAt, null);
  await f.save();
  const at = f.instances.get("d1").ctx.alarmAt;
  f.advance(30_000);
  await f.save();
  assert.equal(f.instances.get("d1").ctx.alarmAt, at);
});

test("published, excluded and failed drafts cancel auto approval", async t => {
  for (const status of ["published", "excluded", "failed"]) {
    const f = fixture(t);
    await f.save();
    await f.save({ ...f.draft, status, url: "https://blog.example/post" });
    f.advance(60_000);
    await f.alarm();
    assert.equal(publishes().length, 0);
    if (status === "published") {
      await f.save();
      assert.equal((await f.read()).status, "published");
      assert.equal((await f.read()).url, "https://blog.example/post");
    }
    t.mock.restoreAll();
  }
});

test("a dispatch error retries after one minute, with a five attempt cap", async t => {
  const f = fixture(t);
  await f.save();
  globalThis.fetch = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return String(url).startsWith("https://slack.com/") ? Response.json({ ok: true })
      : new Response("unavailable", { status: 503 });
  };
  for (let i = 1; i <= 5; i++) {
    f.advance(60_000);
    await f.alarm();
    assert.equal((await f.read()).status, "approved");
    assert.equal((await f.read()).dispatch_attempts, i);
  }
  assert.equal(f.instances.get("d1").ctx.alarmAt, null);
  assert.equal(publishes().length, 5);
  await f.alarm(); // 알람 재전송도 재시도 상한을 우회하지 않는다
  assert.equal(publishes().length, 5);
});

test("the deadline is measured from the Slack post, not from the last upload", async t => {
  const f = fixture(t);
  f.advance(30_000); // Slack 게시 뒤 파일 업로드에 30초가 걸림
  await f.save();
  assert.equal(f.instances.get("d1").ctx.alarmAt, Date.now() + 30_000);
  f.advance(30_000);
  await f.alarm();
  assert.equal(publishes().length, 1);
});

test("a hold during attachment upload survives the final ready save", async t => {
  const f = fixture(t);
  await f.save({ ...f.draft, approval_ready: false });
  assert.equal(f.instances.get("d1").ctx.alarmAt, null);
  assert.equal(await f.manual("x"), "held");
  f.advance(90_000);
  await f.save({ ...f.draft, approval_ready: true });
  await f.alarm();
  assert.equal((await f.read()).status, "held");
  assert.equal(publishes().length, 0);
});

test("an interrupted dispatch is resumed; disabling auto approval cancels pending alarms", async t => {
  const f = fixture(t);
  await f.save();
  const item = f.instances.get("d1");
  const record = await item.ctx.storage.get("approval");
  await item.ctx.storage.put("approval", { ...record, status: "approved", dispatch_pending: true, approved_by: "U1" });
  await f.alarm();
  assert.equal(publishes().length, 1);
  assert.equal((await f.read()).dispatch_pending, false);
  await f.save(f.draft, "d2");
  f.env.AUTO_APPROVE_ENABLED = "false";
  f.advance(60_000);
  await f.env.DRAFT_APPROVAL.get("d2").alarm();
  assert.equal(publishes().length, 1);
});

test("health reports the policy and missing timer binding fails closed", async t => {
  const f = fixture(t);
  assert.deepEqual(await (await worker.fetch(new Request("https://test/health"), f.env)).json(),
    { ok: true, auto_approve: true, auto_approve_seconds: 60, durable_approval: true });
  delete f.env.DRAFT_APPROVAL;
  assert.equal((await f.save()).status, 503);
  assert.equal((await worker.fetch(new Request("https://test/draft/d1", { headers: { Authorization: "Bearer wrong" } }), f.env)).status, 401);
});

const WATCH_ID = "C0MANUAL01-1791244200000001";
function watchRequest(f, input = {}, token = "test") {
  return worker.fetch(new Request(`https://test/manual-watch/${WATCH_ID}`, { method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: JSON.stringify({ user: "U1", created: new Date(Date.now()).toISOString(), ...input }),
  }), f.env);
}

test("manual start watchdog survives restart, preserves the first deadline and checks once", async t => {
  const f = fixture(t);
  await f.save(); // 승인 타이머와 수동 지시 타이머는 별도 객체다.
  const approvalAt = f.instances.get("d1").ctx.alarmAt;
  const response = await watchRequest(f);
  assert.equal(response.status, 201);
  const due = Date.parse((await response.json()).due_at);
  const item = f.instances.get(`manual-watch:${WATCH_ID}`);
  f.advance(60_000);
  assert.equal((await watchRequest(f)).status, 200);
  assert.equal(item.ctx.alarmAt, due);
  item.obj = new DraftApproval(item.ctx, f.env);
  f.advance(14 * 60_000);
  await item.obj.alarm();
  assert.equal(calls.filter(c => c.url.includes("/codex_fallback.yml/dispatches")).length, 0);
  f.advance(1000);
  await item.obj.alarm();
  await item.obj.alarm();
  const checks = calls.filter(c => c.url.includes("/codex_fallback.yml/dispatches"));
  assert.equal(checks.length, 1);
  assert.deepEqual(checks[0].body.inputs, { mode: "work" });
  assert.equal(item.ctx.alarmAt, null);
  assert.equal(f.instances.get("d1").ctx.alarmAt, approvalAt);
  assert.equal((await watchRequest(f)).status, 200);
  assert.equal(item.ctx.alarmAt, null); // 재전송은 종료된 타이머를 다시 켜지 않는다.
});

test("manual watchdog requires the upload secret, owner and a recent timezone-qualified time", async t => {
  const f = fixture(t);
  assert.equal((await watchRequest(f, {}, "wrong")).status, 401);
  assert.equal((await watchRequest(f, { user: "U2" })).status, 403);
  for (const created of ["bad", "2026-10-06T10:00:00", new Date(Date.now() + 1000).toISOString(),
    new Date(Date.now() - 7 * 60 * 60_000).toISOString()]) {
    assert.equal((await watchRequest(f, { created })).status, 400);
  }
  f.env.CODEX_FALLBACK_ENABLED = "false";
  assert.equal((await watchRequest(f)).status, 503);
});

test("manual watchdog retries failed GitHub requests five times and reports failure in the original thread", async t => {
  const f = fixture(t);
  await watchRequest(f);
  const item = f.instances.get(`manual-watch:${WATCH_ID}`);
  globalThis.fetch = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return String(url).startsWith("https://slack.com/") ? Response.json({ ok: true })
      : new Response("unavailable", { status: 503 });
  };
  f.advance(15 * 60_000 + 1000);
  for (let i = 0; i < 5; i++) {
    await item.obj.alarm();
    f.advance(60_000);
  }
  await item.obj.alarm();
  assert.equal(calls.filter(c => c.url.includes("/codex_fallback.yml/dispatches")).length, 5);
  assert.equal(item.ctx.alarmAt, null);
  const notice = calls.find(c => c.url.endsWith("/chat.postMessage"));
  assert.equal(notice.body.channel, "C0MANUAL01");
  assert.equal(notice.body.thread_ts, "1791244200.000001");
  assert.match(notice.body.text, /5회/);
});

test("durable Slack retries and concurrent duplicates queue one request until the persisted receipt", async t => {
  const f = fixture(t);
  f.env.MANUAL_CHANNEL_ID = "C0MANUAL01";
  const raw = JSON.stringify({ type: "event_callback", event: { type: "message", channel: "C0MANUAL01",
    ts: "1791244200.000001", user: "U1", text: "전체 새로운 포스트로 재발행" } });
  const retry = new Request("https://test/slack/events", { headers: { "X-Slack-Retry-Num": "1" } });
  await Promise.all(Array.from({ length: 10 }, () => handleSlackEvent(retry, f.env, {}, raw)));
  assert.equal(f.instances.size, 1);
  assert.equal(calls.length, 0); // 접수 응답은 네트워크 dispatch 전에 영속 저장만 한다.
  const [name, item] = [...f.instances][0];
  item.obj = new DraftApproval(item.ctx, f.env);
  f.advance(1000);
  await item.obj.alarm();
  assert.equal(calls.length, 1);
  const id = name.replace("delivery:", "");
  assert.equal(calls[0].body.inputs.delivery_id, id);
  f.advance(10 * 60_000);
  await item.obj.alarm(); // HTTP 204를 받았어도 Python 기록 ACK가 없으면 같은 ID로 재전송.
  assert.equal(calls.length, 2);
  assert.equal(calls[1].body.inputs.delivery_id, id);
  assert.equal((await worker.fetch(new Request(`https://test/delivery/${id}`, { method: "POST",
    headers: { Authorization: "Bearer test" } }), f.env)).status, 200);
  await item.obj.alarm();
  await handleSlackEvent(retry, f.env, {}, raw);
  assert.equal(calls.length, 2);
  assert.equal(item.ctx.alarmAt, null);
});

test("durable queue storage failure returns 503 so Slack can retry instead of losing the command", async t => {
  const f = fixture(t);
  f.env.MANUAL_CHANNEL_ID = "C0MANUAL01";
  const get = f.env.DRAFT_APPROVAL.get;
  let broken = true;
  f.env.DRAFT_APPROVAL.get = id => {
    const obj = get(id), item = f.instances.get(id);
    item.ctx.storage.setAlarm = async at => { if (broken) throw new Error("storage unavailable"); item.ctx.alarmAt = at; };
    return obj;
  };
  const raw = JSON.stringify({ type: "event_callback", event: { type: "message", channel: "C0MANUAL01",
    ts: "1791244200.000001", user: "U1", text: "AI 도구 한 편 더 발행해줘" } });
  assert.equal((await handleSlackEvent(new Request("https://test"), f.env, {}, raw)).status, 503);
  const item = [...f.instances.values()][0];
  assert.equal(await item.ctx.storage.get("delivery"), undefined); // transaction rollback
  broken = false;
  assert.equal((await handleSlackEvent(new Request("https://test"), f.env, {}, raw)).status, 200);
  assert.equal((await item.ctx.storage.get("delivery")).status, "queued");
  assert.equal(item.ctx.alarmAt, Date.now() + 1000);
});

test("delivery timeout is retried with stable ID, capped, and failure notice survives Slack downtime", async t => {
  const f = fixture(t);
  const id = await enqueueManual(f.env, { text: "AI 도구 한 편 더 발행해줘", user: "U1", source: "slash", channel: "CMANUAL" }, "trigger1");
  const item = f.instances.get(`delivery:${id}`);
  let slackDown = true;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    if (String(url).startsWith("https://slack.com/")) return Response.json({ ok: !slackDown });
    throw new Error("dispatch response lost");
  };
  for (let i = 0; i < 5; i++) { f.advance(60_000); await item.obj.alarm(); }
  await item.obj.alarm();
  assert.equal((await item.ctx.storage.get("delivery")).attempts, 5);
  assert.equal(calls.filter(c => !c.url.startsWith("https://slack.com/")).length, 5);
  assert.equal(item.ctx.alarmAt, Date.now() + 15 * 60_000);
  slackDown = false;
  f.advance(15 * 60_000);
  await item.obj.alarm();
  await item.obj.alarm();
  assert.equal(item.ctx.alarmAt, null);
  assert.equal((await item.ctx.storage.get("delivery")).failure_notified, true);
  assert.equal(calls.filter(c => c.url.endsWith("chat.postMessage")).length, 2);
});

test("delivery status and receipt reject missing upload secret", async t => {
  const f = fixture(t);
  const id = await enqueueManual(f.env, { text: "AI 도구 한 편 더 발행해줘", user: "U1", source: "slash" }, "trigger2");
  for (const method of ["GET", "POST"]) {
    assert.equal((await worker.fetch(new Request(`https://test/delivery/${id}`, { method }), f.env)).status, 401);
  }
  assert.equal((await worker.fetch(new Request(`https://test/delivery/${id}`, {
    headers: { Authorization: "Bearer test" } }), f.env)).status, 200);
});

test("approved publication missing completion is watched; retries preserve approver and send auto notice once", async t => {
  const f = fixture(t);
  await f.save(); f.advance(60_000); await f.alarm();
  const approvedAt = (await f.read()).approved_at;
  f.advance(15 * 60_000);
  await f.alarm();
  assert.equal(publishes().length, 2);
  assert.equal((await f.read()).approved_at, approvedAt);
  assert.equal((await f.read()).approved_by, "auto-60s");
  assert.equal(calls.filter(c => c.url.endsWith("chat.postMessage")).length, 1);
  await f.save({ ...f.draft, status: "published", url: "https://b/p/1" });
  await f.save({ ...f.draft, status: "failed", url: "" }); // 유실 응답을 처리한 늦은 runner
  f.advance(15 * 60_000); await f.alarm();
  assert.equal(publishes().length, 2);
  assert.equal((await f.read()).status, "published");
  assert.equal((await f.read()).url, "https://b/p/1");
});

test("publication watch retries failed approved work and stops after five dispatches with explicit resume", async t => {
  const f = fixture(t);
  await f.save(); await f.manual();
  for (let i = 1; i < 5; i++) {
    await f.save({ ...f.draft, status: "failed", approved_by: "U1" });
    f.advance(15 * 60_000); await f.alarm();
  }
  assert.equal(publishes().length, 5);
  f.advance(15 * 60_000); await f.alarm(); await f.alarm();
  assert.equal((await f.read()).publish_alerted, true);
  assert.equal(f.instances.get("d1").ctx.alarmAt, null);
  assert.equal(calls.filter(c => c.url.endsWith("chat.postMessage")).length, 1);
  assert.equal(await f.manual(), "dispatched");
  assert.equal(publishes().length, 6);
  await f.save({ ...f.draft, status: "published", url: "https://b/p/1" });
  assert.equal(await f.manual(), "already handled");
});

test("approval KV write interruption keeps a durable alarm and does not dispatch until data recovers", async t => {
  const f = fixture(t);
  await f.save();
  const put = f.env.IMAGES.put;
  let down = true;
  f.env.IMAGES.put = async (...a) => { if (down) throw new Error("KV unavailable"); return put(...a); };
  await assert.rejects(f.manual(), /KV unavailable/);
  assert.equal((await f.read()).dispatch_pending, true);
  assert.equal(publishes().length, 0);
  down = false;
  f.advance(60_000); await f.alarm();
  assert.equal(publishes().length, 1);
  assert.equal((await f.read()).approved_by, "U1");
});

test("held work cannot be revived by stale approved or failed saves", async t => {
  const f = fixture(t);
  await f.save(); await f.manual("x");
  await f.save({ ...f.draft, status: "failed", approved_by: "auto-60s" });
  await f.save({ ...f.draft, status: "approved", approved_by: "auto-60s" });
  f.advance(15 * 60_000); await f.alarm();
  assert.equal((await f.read()).status, "held");
  assert.equal(publishes().length, 0);
});
