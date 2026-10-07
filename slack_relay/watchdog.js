// GitHub의 실행 서버가 배정되지 않아도 Cloudflare가 별도로 대기를 감시한다.
const MINUTE = 60_000;
const STALE = 15 * MINUTE;
const INTERVAL = 30 * MINUTE;
const WORKFLOWS = ["codex_fallback.yml", "cloud_drafts.yml"];
const ACTIVE = ["in_progress", "queued", "waiting", "pending"];

export class CloudWatchdog {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; }

  async fetch(request) {
    if (request.method === "GET") {
      return Response.json(await this.ctx.storage.get("state") || { status: "not_checked" });
    }
    if (request.method !== "POST") return new Response("method not allowed", { status: 405 });
    return this.ctx.blockConcurrencyWhile(async () => {
      // Durable Object의 30초 동시성 잠금 제한 전에 점검을 끝낸다.
      this.deadline = Date.now() + 20_000;
      const state = await this.ctx.storage.get("state") || { workflows: {} };
      state.workflows ||= {};
      state.checked_at = new Date().toISOString();
      try {
        for (const workflow of WORKFLOWS) {
          if (workflow === "codex_fallback.yml" && this.env.CODEX_FALLBACK_ENABLED !== "true") continue;
          await this.check(workflow, state);
        }
        state.status = "ok";
        state.error = "";
      } catch (error) {
        state.status = "error";
        state.error = error.message.slice(0, 150);
        await this.notice(state, `⚠️ YooPost 클라우드 복구 점검 실패: ${state.error}`);
      }
      await this.save(state);
      return Response.json(state);
    });
  }

  async save(state) { await this.ctx.storage.put("state", state); }

  async api(path, method = "GET", body) {
    if (!this.env.GITHUB_TOKEN || !this.env.GITHUB_REPO) throw new Error("GitHub 인증 설정 필요");
    const remaining = this.deadline - Date.now();
    if (remaining <= 0) throw new Error("클라우드 점검 시간 초과 — 다음 예약에서 재확인");
    const response = await fetch(`https://api.github.com/repos/${this.env.GITHUB_REPO}/${path}`, {
      method,
      headers: { Authorization: `Bearer ${this.env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "yoopost-watchdog", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(Math.min(5000, remaining)),
    });
    // 409는 취소 직전 작업 상태가 바뀐 경우도 포함한다. 다음 점검에서 다시 확인한다.
    if (method === "POST" && path.endsWith("/cancel") && response.status === 409) return false;
    if (!response.ok) throw new Error(`GitHub HTTP ${response.status} (${method} ${path.split("?")[0]})`);
    return response.status === 204 || response.status === 202 ? true : response.json();
  }

  async jobs(run) {
    const data = await this.api(`actions/runs/${run.id}/jobs?per_page=100`);
    if (data.total_count > 100) throw new Error("실행 단계가 너무 많아 자동 취소하지 않았습니다");
    return data.jobs || [];
  }

  stale(run, jobs, now) {
    if (run.status === "completed" || jobs.some(j => j.status === "in_progress")) return false;
    const queued = jobs.filter(j => ACTIVE.includes(j.status));
    // 대기 단계가 하나라도 새로 생겼으면 시작할 시간을 준다.
    const finished = jobs.map(j => Date.parse(j.completed_at)).filter(Number.isFinite);
    const betweenJobs = finished.length ? new Date(Math.max(...finished)).toISOString() : run.created_at;
    const dates = queued.length ? queued.map(j => j.created_at || run.created_at) : [betweenJobs];
    return dates.every(date => Number.isFinite(Date.parse(date)) && now - Date.parse(date) >= STALE);
  }

  async permit(item, state, now) {
    item.actions = (item.actions || []).filter(at => now - at < 60 * MINUTE);
    if (item.actions.length >= 6) {
      item.status = "backoff";
      await this.notice(state, "⚠️ YooPost 실행 대기가 반복되어 자동 복구를 1시간 동안 제한합니다. 다음 점검에서 다시 시도합니다.");
      return false;
    }
    // API 응답 유실·Worker 재시작에도 요청 횟수가 늘어나지 않게 먼저 저장한다.
    item.actions.push(now);
    await this.save(state);
    return true;
  }

  async check(workflow, state) {
    const now = Date.now();
    const item = state.workflows[workflow] ||= {};
    const branch = encodeURIComponent(this.env.GITHUB_REF || "main");
    const runs = new Map();
    const snapshots = await Promise.all(ACTIVE.map(status =>
      this.api(`actions/workflows/${workflow}/runs?branch=${branch}&status=${status}&per_page=5`)));
    for (const data of snapshots) {
      for (const run of data.workflow_runs || []) runs.set(run.id, run);
      if (data.total_count > 5) {
        item.status = "backlog";
        await this.notice(state, `⚠️ YooPost ${workflow}: 대기 작업이 많아 실행 기록 확인이 필요합니다.`);
        return;
      }
    }
    const running = [...runs.values()].filter(run => run.status === "in_progress");
    // 실제 서버에서 수행 중인 작업을 취소하지 않는다. 같은 그룹의 후속 대기도 유지한다.
    for (const run of running) {
      const jobs = await this.jobs(run);
      if (jobs.some(j => j.status === "in_progress")) {
        item.status = "running";
        item.run = run.id;
        if (item.recovering) {
          await this.notice(state, `✅ YooPost 클라우드 작업에 실행 서버가 배정되어 작업이 재개됐습니다. ${run.html_url}`);
          item.recovering = false;
        }
        return;
      }
    }
    for (const run of [...runs.values()].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at))) {
      const jobs = await this.jobs(run);
      if (!this.stale(run, jobs, now)) continue;
      const deployments = await this.api(`actions/runs/${run.id}/pending_deployments`);
      if (deployments.some(d => d.reviewers?.length || d.wait_timer > 0)) {
        item.status = "approval_required";
        await this.notice(state, `⚠️ YooPost GitHub 배포 승인 대기: ${run.html_url}`);
        return;
      }
      // 조회와 취소 사이에 서버가 배정될 수 있으므로 직전에 다시 확인한다.
      const fresh = await this.api(`actions/runs/${run.id}`);
      if (!this.stale(fresh, await this.jobs(fresh), Date.now())) continue;
      if (item.cancel_requested === run.id && now - item.cancel_at < STALE) {
        item.status = "cancelling";
        return;
      }
      if (!await this.permit(item, state, now)) return;
      item.cancel_requested = run.id;
      item.cancel_at = now;
      item.recovering = true;
      await this.save(state);
      if (await this.api(`actions/runs/${run.id}/cancel`, "POST")) {
        item.status = "cancelling";
        await this.notice(state, `🤖🔄 YooPost 서버 미배정 대기(15분 이상)를 감지해 복구 중입니다. ${run.html_url}`);
      }
      return; // 취소가 실제 완료될 때까지 새 작성기를 요청하지 않는다.
    }
    if (runs.size) { item.status = "queued"; return; }
    const latest = await this.api(`actions/workflows/${workflow}/runs?branch=${branch}&per_page=1`);
    const last = latest.workflow_runs?.[0];
    const lastAt = Math.max(Date.parse(last?.created_at) || 0, item.dispatch_at || 0);
    const kstMinute = ((new Date(now).getUTCHours() + 9) % 24) * 60 + new Date(now).getUTCMinutes();
    if (workflow === "codex_fallback.yml" && kstMinute < 7 * 60 + 15) { item.status = "morning_wait"; return; }
    if (!item.recovering && now - lastAt < INTERVAL) { item.status = "idle"; return; }
    if (now - (item.dispatch_at || 0) < 5 * MINUTE) { item.status = "dispatch_wait"; return; }
    if (!await this.permit(item, state, now)) return;
    item.dispatch_at = now;
    item.status = "dispatching";
    await this.save(state);
    await this.api(`actions/workflows/${workflow}/dispatches`, "POST", {
      ref: this.env.GITHUB_REF || "main",
      inputs: workflow === "codex_fallback.yml" ? { mode: "work" } : { note: "Cloudflare 예약·실행 대기 복구 점검" },
    });
    item.status = "dispatched";
  }

  async notice(state, text) {
    state.notices ||= {};
    state.notices = Object.fromEntries(Object.entries(state.notices).filter(([, at]) => Date.now() - at < 24 * 60 * MINUTE));
    if (state.notices[text] || !this.env.SLACK_BOT_TOKEN || !this.env.WATCHDOG_CHANNEL_ID) return;
    const response = await fetch("https://slack.com/api/chat.postMessage", {
      method: "POST", headers: { Authorization: `Bearer ${this.env.SLACK_BOT_TOKEN}`, "Content-Type": "application/json" },
      body: JSON.stringify({ channel: this.env.WATCHDOG_CHANNEL_ID, text }), signal: AbortSignal.timeout(5000),
    }).catch(() => null);
    if (response?.ok && (await response.json().catch(() => ({}))).ok) {
      state.notices[text] = Date.now();
      await this.save(state);
    }
  }
}
