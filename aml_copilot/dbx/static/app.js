// The page talks only to this app's own /api. Server text is always written with textContent, never innerHTML.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const HEADERS = { "Content-Type": "application/json", "X-Requested-With": "aml-copilot" };
  const PROVIDER_NAMES = { gemini: "Gemini", claude: "Claude" };
  let me = null, openId = null;
  const ticked = [];           // up to two investigation ids picked for comparison, oldest first
  const timers = new Set();

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
  const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
  const providerName = (job) => PROVIDER_NAMES[job.provider] || (job.result && job.result.provenance.llm_provider) || "-";

  // -------------------------------------------------------------------------------------------- who am I
  async function init() {
    try {
      me = await api("/api/me");
      $("who").textContent = "";
      $("who").append("Signed in as ", el("strong", me.email), ` (${me.role})`);
      await loadList();
    } catch (error) { $("who").textContent = error.message; say("start-msg", error.message, true); $("start-btn").disabled = true; }
  }

  // ----------------------------------------------------------------------------------------- the list
  const REVIEW = { PENDING_REVIEW: "awaiting review", APPROVED: "approved", REJECTED: "rejected", ESCALATED: "escalated", NOT_REQUIRED: "" };
  const statusText = (item) => item.status !== "COMPLETED" ? item.status.toLowerCase().replace("_", " ")
    : [(item.outcome || "").replace("_", " "), REVIEW[item.review_status]].filter(Boolean).join(" · ");

  async function loadList() {
    const items = await api("/api/investigations");
    const body = $("rows"); body.replaceChildren();
    if (!items.length) { const tr = body.insertRow(); const td = tr.insertCell(); td.colSpan = 6; td.className = "small"; td.textContent = "Nothing yet. Start an investigation above."; return; }
    for (const item of items) {
      const tr = body.insertRow(); tr.tabIndex = 0; tr.dataset.id = item.job_id;
      if (item.job_id === openId) tr.className = "open";
      const box = document.createElement("input");
      box.type = "checkbox"; box.checked = ticked.includes(item.job_id); box.style.width = "auto";
      box.setAttribute("aria-label", `Pick ${item.account_id} ${item.created_at} for comparison`);
      box.onclick = (e) => e.stopPropagation();
      box.onchange = () => tick(item.job_id, box.checked);
      tr.insertCell().append(box);
      tr.insertCell().textContent = when(item.created_at);
      const acc = tr.insertCell(); acc.className = "id"; acc.textContent = item.account_id;
      tr.insertCell().textContent = PROVIDER_NAMES[item.provider] || "-";
      const t = el("span", item.tier || "-", `tier ${item.tier || ""}`); tr.insertCell().append(t);
      const st = tr.insertCell(); st.style.whiteSpace = "normal"; st.textContent = statusText(item);
      const open = () => openInvestigation(item.job_id);
      tr.onclick = open; tr.onkeydown = (e) => { if (e.key === "Enter") open(); };
    }
  }

  function tick(id, on) {
    const at = ticked.indexOf(id);
    if (on && at < 0) ticked.push(id);
    if (!on && at >= 0) ticked.splice(at, 1);
    while (ticked.length > 2) ticked.shift();                     // keep the two most recent picks
    document.querySelectorAll("#rows input[type=checkbox]").forEach((b) => { b.checked = ticked.includes(b.closest("tr").dataset.id); });
    $("compare-btn").disabled = ticked.length !== 2;
    $("compare-btn").textContent = ticked.length === 2 ? "Compare selected (2)" : `Compare selected (${ticked.length}/2)`;
  }

  // ---------------------------------------------------------------------------- the pieces of one draft
  function factBlock(result) {
    const box = el("div", undefined, "small"); const f = result && result.faithfulness;
    if (!f) { box.textContent = result && result.faithfulness_error ? `Could not run: ${result.faithfulness_error}` : "Not run."; return box; }
    const line = el("div", `${f.supported} sentences supported, ${f.unsupported} unsupported, ${f.contradicted} contradicted, ${f.unverified} unverified. `);
    line.append(el("strong", f.passed ? "PASSED" : "NEEDS ATTENTION", f.passed ? "good" : "bad")); box.append(line);
    for (const c of f.checks.filter((x) => x.verdict !== "supported")) box.append(el("div", `Sentence ${c.number} (${c.verdict}): ${c.text} - ${c.reason}`));
    for (const issue of f.consistency_issues) box.append(el("div", `Consistency: ${issue}`));
    return box;
  }
  const warningsList = (result) => { const ul = el("ul"); for (const w of (result && result.warnings) || []) ul.append(el("li", w)); return ul; };
  const provenanceText = (p) => p ? `${p.llm_provider}: ${Object.entries(p.models).map(([k, v]) => `${k}=${v}`).join(", ")}; ${Object.entries(p.step_seconds).map(([k, v]) => `${k} ${v}s`).join(", ")}` : "";

  // ---------------------------------------------------------------------------------------- one case
  async function openInvestigation(id) {
    openId = id; say("decision-msg", "");
    $("empty").classList.add("hidden"); $("detail").classList.remove("hidden");
    $("d-title").textContent = "Loading..."; $("d-narrative").textContent = "";
    document.querySelectorAll("#rows tr").forEach((r) => r.classList.toggle("open", r.dataset.id === id));
    const job = await api(`/api/investigations/${id}`);
    const result = job.result;
    $("d-title").textContent = `${job.account_id}  ·  ${providerName(job)}  ·  ${id.slice(0, 8)}`;
    const pills = $("d-pills"); pills.replaceChildren();
    const pill = (text, cls = "") => pills.append(el("span", text, `pill ${cls}`));
    pill(job.status.toLowerCase());
    if (result) { pill(result.status.replace("_", " ")); const tier = (result.risk_score || {}).tier; if (tier) pill(`risk ${tier}`, `tier ${tier}`); }
    pill(`review: ${job.review_status.toLowerCase().replace("_", " ")}`);
    $("d-detail").textContent = result ? result.status_detail : (job.error || "Working...");
    $("d-narrative").textContent = result && result.narrative_text ? result.narrative_text : "(no draft)";
    $("d-fact").replaceChildren(factBlock(result));
    $("d-warnings").replaceChildren(...warningsList(result).children);
    $("d-prov").textContent = provenanceText(result && result.provenance);

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
      box.append(el("div", a.chain_ok ? "Audit chain verified." : `AUDIT CHAIN BROKEN: ${a.chain_problem}`, a.chain_ok ? "good" : "bad"));
      const ul = el("ul");
      for (const e of a.events) ul.append(el("li", `#${e.seq} ${e.event_type} by ${e.actor} at ${e.at.slice(0, 19)}`));
      box.append(ul);
    } catch (error) { box.textContent = error.message; }
  }

  // ------------------------------------------------------------------------------------ start run(s)
  async function startInvestigation() {
    const account = $("account").value.trim().toUpperCase();
    const choice = $("provider").value;
    const providers = choice === "both" ? ["gemini", "claude"] : [choice];
    say("start-msg", "Starting..."); $("start-btn").disabled = true;
    try {
      const since = new Date().toISOString().slice(0, 19);
      const runs = [];
      for (const provider of providers) runs.push({ provider, run: await api("/api/investigations", { method: "POST", body: JSON.stringify({ account_id: account, provider }) }) });
      say("start-msg", `${runs.length === 2 ? "Both runs are" : "Run is"} queued. Each takes about five minutes, one after the other.`);
      const found = await Promise.all(runs.map((r) => watch(r.run.run_id, account, since, r.provider, runs.length)));
      $("start-btn").disabled = false;
      await loadList();
      if (found.length === 2 && found.every(Boolean)) { tick(found[0], true); tick(found[1], true); await loadList(); compare(found[0], found[1]); }
      else if (found[0]) openInvestigation(found[0]);
    } catch (error) { say("start-msg", error.message, true); $("start-btn").disabled = false; }
  }

  // Resolves with the investigation id once the run has ended (or null if it failed to produce one).
  function watch(runId, account, since, provider, total) {
    return new Promise((resolve) => {
      const poll = async () => {
        try {
          const s = await api(`/api/runs/${runId}`);
          if (["TERMINATED", "INTERNAL_ERROR", "SKIPPED"].includes(s.state)) {
            if (s.result !== "SUCCESS") say("start-msg", `The ${PROVIDER_NAMES[provider]} run ended: ${s.result || s.state}. ${s.message}`, true);
            const mine = (await api("/api/investigations")).find((i) => i.account_id === account && i.provider === provider && i.created_at >= since);
            return resolve(mine ? mine.job_id : null);
          }
          if (total === 1) say("start-msg", `Run ${runId}: ${String(s.state).toLowerCase()}...`);
          timers.add(setTimeout(poll, 5000));
        } catch (error) { say("start-msg", error.message, true); resolve(null); }
      };
      timers.add(setTimeout(poll, 5000));
    });
  }

  // ------------------------------------------------------------------------------- side by side
  const typologies = (r) => r && r.assessment ? r.assessment.matches.map((m) => m.source_document.replace(".md", "").replace(/_/g, " ")).sort().join(", ") || "none" : "-";
  const supported = (r) => r && r.faithfulness ? `${r.faithfulness.supported} of ${r.faithfulness.checks.length}` : "-";
  const total = (r) => r && r.provenance && r.provenance.step_seconds ? `${r.provenance.step_seconds.total ?? "-"} s` : "-";
  const models = (r) => r ? Object.entries(r.provenance.models).map(([k, v]) => `${k}: ${v}`).join("; ") : "-";

  async function compare(idA, idB) {
    const [a, b] = await Promise.all([api(`/api/investigations/${idA}`), api(`/api/investigations/${idB}`)]);
    $("compare").classList.remove("hidden");
    $("cmp-title").textContent = `Comparison: ${a.account_id}${a.account_id === b.account_id ? "" : " vs " + b.account_id}`;
    const rows = [
      ["Model family", providerName(a), providerName(b)],
      ["Models used", models(a.result), models(b.result)],
      ["Outcome", a.result ? a.result.status.replace("_", " ") : a.status.toLowerCase(), b.result ? b.result.status.replace("_", " ") : b.status.toLowerCase()],
      ["Risk tier", (a.result && a.result.risk_score || {}).tier || "-", (b.result && b.result.risk_score || {}).tier || "-"],
      ["Typologies matched", typologies(a.result), typologies(b.result)],
      ["Sentences supported", supported(a.result), supported(b.result)],
      ["Fact check", a.result && a.result.faithfulness ? (a.result.faithfulness.passed ? "passed" : "needs attention") : "-", b.result && b.result.faithfulness ? (b.result.faithfulness.passed ? "passed" : "needs attention") : "-"],
      ["Warnings", String(a.result ? a.result.warnings.length : "-"), String(b.result ? b.result.warnings.length : "-")],
      ["Time", total(a.result), total(b.result)],
    ];
    const body = $("cmp-metrics"); body.replaceChildren();
    for (const [label, x, y] of rows) {
      const tr = body.insertRow(); tr.insertCell().textContent = label;
      tr.insertCell().textContent = x; tr.insertCell().textContent = y;
      if (x !== y && label !== "Model family" && label !== "Models used") for (const td of [tr.cells[1], tr.cells[2]]) td.className = "diff";
    }
    for (const [target, job] of [["cmp-a", a], ["cmp-b", b]]) {
      const col = $(target); col.replaceChildren();
      col.append(el("h3", `${providerName(job)} - ${job.job_id.slice(0, 8)}`),
        el("pre", job.result && job.result.narrative_text ? job.result.narrative_text : "(no draft)"),
        el("h3", "Fact check"), factBlock(job.result), warningsList(job.result));
    }
    $("compare").scrollIntoView({ behavior: "smooth", block: "start" });
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
  $("compare-btn").onclick = () => compare(ticked[0], ticked[1]).catch((e) => say("start-msg", e.message, true));
  $("cmp-close").onclick = () => $("compare").classList.add("hidden");
  $("approve-btn").onclick = () => decide("approve");
  $("escalate-btn").onclick = () => decide("escalate");
  $("reject-btn").onclick = () => decide("reject");
  init();
})();
