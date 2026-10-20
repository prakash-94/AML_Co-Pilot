// The page talks only to this app's own /api. Server text is always written with textContent, never innerHTML.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const HEADERS = { "Content-Type": "application/json", "X-Requested-With": "aml-copilot" };
  let me = null, openId = null, pollTimer = null;

  async function api(path, options = {}) {
    const response = await fetch(path, { headers: HEADERS, ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
      throw new Error(detail || `Request failed (${response.status})`);
    }
    return body;
  }
  const say = (id, text, bad = false) => { const e = $(id); e.textContent = text; e.className = bad ? "msg err" : "msg"; };
  const when = (iso) => (iso || "").slice(5, 16).replace("T", " ");

  // -------------------------------------------------------------------------------------------- who am I
  async function init() {
    try {
      me = await api("/api/me");
      $("who").textContent = "";
      const strong = document.createElement("strong"); strong.textContent = me.email;
      $("who").append("Signed in as ", strong, ` (${me.role})`);
      await loadList();
    } catch (error) { $("who").textContent = error.message; say("start-msg", error.message, true); $("start-btn").disabled = true; }
  }

  const REVIEW = { PENDING_REVIEW: "awaiting review", APPROVED: "approved", REJECTED: "rejected", ESCALATED: "escalated", NOT_REQUIRED: "" };
  const statusText = (item) => item.status !== "COMPLETED" ? item.status.toLowerCase().replace("_", " ")
    : [(item.outcome || "").replace("_", " "), REVIEW[item.review_status]].filter(Boolean).join(" · ");

  // ----------------------------------------------------------------------------------------- the list
  async function loadList() {
    const items = await api("/api/investigations");
    const body = $("rows"); body.replaceChildren();
    if (!items.length) { const tr = body.insertRow(); const td = tr.insertCell(); td.colSpan = 4; td.className = "small"; td.textContent = "Nothing yet. Start an investigation above."; return; }
    for (const item of items) {
      const tr = body.insertRow(); tr.tabIndex = 0; tr.dataset.id = item.job_id;
      if (item.job_id === openId) tr.className = "open";
      tr.insertCell().textContent = when(item.created_at);
      const acc = tr.insertCell(); acc.className = "id"; acc.textContent = item.account_id;
      const tier = tr.insertCell(); const t = document.createElement("span");
      t.className = `tier ${item.tier || ""}`; t.textContent = item.tier || "-"; tier.append(t);
      const st = tr.insertCell(); st.style.whiteSpace = "normal"; st.textContent = statusText(item);
      const open = () => openInvestigation(item.job_id);
      tr.onclick = open; tr.onkeydown = (e) => { if (e.key === "Enter") open(); };
    }
  }

  // ---------------------------------------------------------------------------------------- one case
  async function openInvestigation(id) {
    openId = id; say("decision-msg", "");
    $("empty").classList.add("hidden"); $("detail").classList.remove("hidden");
    $("d-title").textContent = "Loading..."; $("d-narrative").textContent = "";
    document.querySelectorAll("#rows tr").forEach((r) => r.classList.toggle("open", r.dataset.id === id));
    const job = await api(`/api/investigations/${id}`);
    const result = job.result;
    $("empty").classList.add("hidden"); $("detail").classList.remove("hidden");
    $("d-title").textContent = `${job.account_id}  ·  ${id.slice(0, 8)}`;
    const pills = $("d-pills"); pills.replaceChildren();
    const pill = (text, cls = "") => { const p = document.createElement("span"); p.className = `pill ${cls}`; p.textContent = text; pills.append(p); };
    pill(job.status.toLowerCase());
    if (result) { pill(result.status.replace("_", " ")); const tier = (result.risk_score || {}).tier; if (tier) pill(`risk ${tier}`, `tier ${tier}`); }
    pill(`review: ${job.review_status.toLowerCase().replace("_", " ")}`);
    $("d-detail").textContent = result ? result.status_detail : (job.error || "Working...");
    $("d-narrative").textContent = result && result.narrative_text ? result.narrative_text : "(no draft)";

    const f = result && result.faithfulness;
    const fact = $("d-fact"); fact.replaceChildren();
    if (f) {
      const line = document.createElement("div");
      line.textContent = `${f.supported} sentences supported, ${f.unsupported} unsupported, ${f.contradicted} contradicted, ${f.unverified} unverified. `;
      const verdict = document.createElement("strong"); verdict.className = f.passed ? "good" : "bad"; verdict.textContent = f.passed ? "PASSED" : "NEEDS ATTENTION";
      line.append(verdict); fact.append(line);
      for (const c of f.checks.filter((x) => x.verdict !== "supported")) {
        const d = document.createElement("div"); d.textContent = `Sentence ${c.number} (${c.verdict}): ${c.text} - ${c.reason}`; fact.append(d);
      }
      for (const issue of f.consistency_issues) { const d = document.createElement("div"); d.textContent = `Consistency: ${issue}`; fact.append(d); }
    } else fact.textContent = result && result.faithfulness_error ? `Could not run: ${result.faithfulness_error}` : "Not run.";

    const warnings = $("d-warnings"); warnings.replaceChildren();
    for (const w of (result && result.warnings) || []) { const li = document.createElement("li"); li.textContent = w; warnings.append(li); }

    const p = result && result.provenance;
    $("d-prov").textContent = p ? `${p.llm_provider}: ${Object.entries(p.models).map(([k, v]) => `${k}=${v}`).join(", ")}; ` +
      `${Object.entries(p.step_seconds).map(([k, v]) => `${k} ${v}s`).join(", ")}` : "";

    const pending = job.review_status === "PENDING_REVIEW";
    $("review").classList.toggle("hidden", !pending);
    $("approve-btn").disabled = me.role !== "supervisor";
    $("role-hint").textContent = me.role === "supervisor" ? "" : "Only a supervisor can approve. You can reject or escalate.";
    if (job.decision) say("decision-msg", `Decision: ${job.decision.decision} by ${job.decision.decided_by} (${job.decision.role}) - ${job.decision.comment}`);
    window.currentHash = result ? result.result_hash : "";
    loadAudit(id);
  }

  async function loadAudit(id) {
    const box = $("d-audit"); box.replaceChildren();
    try {
      const a = await api(`/api/investigations/${id}/audit`);
      const status = document.createElement("div"); status.className = a.chain_ok ? "good" : "bad";
      status.textContent = a.chain_ok ? "Audit chain verified." : `AUDIT CHAIN BROKEN: ${a.chain_problem}`; box.append(status);
      const ul = document.createElement("ul");
      for (const e of a.events) { const li = document.createElement("li"); li.textContent = `#${e.seq} ${e.event_type} by ${e.actor} at ${e.at.slice(0, 19)}`; ul.append(li); }
      box.append(ul);
    } catch (error) { box.textContent = error.message; }
  }

  // ------------------------------------------------------------------------------------ start a run
  async function startInvestigation() {
    const account = $("account").value.trim().toUpperCase();
    say("start-msg", "Starting...");
    $("start-btn").disabled = true;
    try {
      const run = await api("/api/investigations", { method: "POST", body: JSON.stringify({ account_id: account }) });
      say("start-msg", `Run ${run.run_id} started. A run takes about five minutes, mostly installing libraries.`);
      watch(run.run_id, account, new Date().toISOString().slice(0, 19));
    } catch (error) { say("start-msg", error.message, true); $("start-btn").disabled = false; }
  }

  function watch(runId, account, since) {
    clearTimeout(pollTimer);
    const tick = async () => {
      try {
        const s = await api(`/api/runs/${runId}`);
        if (s.state === "TERMINATED" || s.state === "INTERNAL_ERROR" || s.state === "SKIPPED") {
          $("start-btn").disabled = false;
          say("start-msg", s.result === "SUCCESS" ? "Finished." : `The run ended: ${s.result || s.state}. ${s.message}`, s.result !== "SUCCESS");
          await loadList();
          const mine = (await api("/api/investigations")).find((i) => i.account_id === account && i.created_at >= since);
          if (mine) openInvestigation(mine.job_id);
          return;
        }
        say("start-msg", `Run ${runId}: ${String(s.state).toLowerCase()}...`);
        pollTimer = setTimeout(tick, 5000);
      } catch (error) { say("start-msg", error.message, true); $("start-btn").disabled = false; }
    };
    pollTimer = setTimeout(tick, 5000);
  }

  // ------------------------------------------------------------------------------------- a decision
  async function decide(kind) {
    const comment = $("comment").value.trim();
    if (!comment) { say("decision-msg", "Please write a comment first.", true); return; }
    try {
      const done = await api(`/api/investigations/${openId}/decision`, { method: "POST", body: JSON.stringify({ decision: kind, comment, result_hash: window.currentHash }) });
      say("decision-msg", done.message);
      await loadList(); await openInvestigation(openId);
    } catch (error) { say("decision-msg", error.message, true); }
  }

  $("start-btn").onclick = startInvestigation;
  $("account").onkeydown = (e) => { if (e.key === "Enter") startInvestigation(); };
  $("refresh-btn").onclick = () => loadList().catch((e) => say("start-msg", e.message, true));
  $("approve-btn").onclick = () => decide("approve");
  $("escalate-btn").onclick = () => decide("escalate");
  $("reject-btn").onclick = () => decide("reject");
  init();
})();
