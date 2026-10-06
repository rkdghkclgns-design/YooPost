import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import worker, { DraftApproval, approveDraft } from "./worker.js";

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
    AUTO_APPROVE_ENABLED: "true", ALLOWED_USER_IDS: "U1", SLACK_BOT_TOKEN: "test",
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
  assert.equal(item.ctx.alarmAt, null);
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
    assert.equal((await f.read()).status, "pending");
    assert.equal((await f.read()).auto_attempts, i);
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
