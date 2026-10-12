// Minimal front end: Firebase sign-in, then plain fetch() calls to /api (same origin via Firebase Hosting).
// Every piece of server text is written with textContent, never innerHTML, so it cannot inject markup.
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import { getAuth, onAuthStateChanged, signInWithEmailAndPassword, signOut }
  from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import { firebaseConfig } from "./config.js";

const auth = getAuth(initializeApp(firebaseConfig));
const $ = (id) => document.getElementById(id);
let currentJob = null;
let pollTimer = null;

function show(id, on) { $(id).classList.toggle("hidden", !on); }
function say(id, text, isError = false) { $(id).textContent = text; $(id).className = isError ? "err" : "muted"; }

async function api(path, options = {}) {
  const token = await auth.currentUser.getIdToken();
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body);
    throw new Error(`${response.status}: ${detail}`);
  }
  return { status: response.status, body };
}

// ---------------------------------------------------------------- sign in / out
onAuthStateChanged(auth, (user) => {
  show("signin", !user);
  show("app", !!user);
  $("who").textContent = user ? user.email : "";
});

$("signin-btn").onclick = async () => {
  $("signin-msg").textContent = "";
  try { await signInWithEmailAndPassword(auth, $("email").value, $("password").value); }
  catch (error) { $("signin-msg").textContent = "Sign-in failed."; }
};
$("signout-btn").onclick = () => { clearTimeout(pollTimer); show("job", false); signOut(auth); };

// ---------------------------------------------------------------- start + poll
$("start-btn").onclick = async () => {
  say("start-msg", "Submitting...");
  try {
    const { status, body } = await api("/investigations", {
      method: "POST", body: JSON.stringify({ account_id: $("account").value }),
    });
    say("start-msg", status === 200 && body.duplicate ? "An investigation of this account is already running." : "Queued.");
    watch(body.job_id);
  } catch (error) { say("start-msg", error.message, true); }
};

function watch(jobId) {
  clearTimeout(pollTimer);
  currentJob = jobId;
  const tick = async () => {
    try {
      const { body } = await api(`/investigations/${jobId}`);
      render(body);
      if (body.status === "QUEUED" || body.status === "RUNNING") pollTimer = setTimeout(tick, 3000);
      else loadAudit(jobId);
    } catch (error) { say("start-msg", error.message, true); }
  };
  tick();
}

// ---------------------------------------------------------------- render
function render(job) {
  const result = job.result;
  show("job", true);
  $("job-account").textContent = job.account_id;
  $("job-status").textContent = job.status === "COMPLETED" ? `${result.status} · ${job.review_status}` : job.status;
  const tier = result && result.risk_score ? result.risk_score.tier : "";
  $("job-tier").textContent = tier ? `risk: ${tier}` : "";
  $("job-tier").className = `pill ${tier}`;
  $("job-detail").textContent = result ? result.status_detail : (job.error || "Working... this usually takes under a minute.");
  $("narrative").textContent = result && result.narrative_text ? result.narrative_text : "(no draft)";

  const warnings = $("warnings");
  warnings.replaceChildren();
  if (result && result.warnings.length) {
    const list = document.createElement("ul");
    for (const w of result.warnings) { const li = document.createElement("li"); li.textContent = w; list.append(li); }
    warnings.append(list);
  }

  const f = result && result.faithfulness;
  if (f) {
    const flagged = f.checks.filter((c) => c.verdict !== "supported");
    const lines = [`${f.supported} sentences supported, ${f.unsupported} unsupported, ${f.contradicted} contradicted, `
      + `${f.unverified} unverified. ${f.passed ? "PASSED" : "NEEDS ATTENTION"}.`];
    for (const c of flagged) lines.push(`Sentence ${c.number} (${c.verdict}): ${c.text} — ${c.reason}`);
    for (const issue of f.consistency_issues) lines.push(`Consistency: ${issue}`);
    $("faithfulness").textContent = lines.join("\n");
    $("faithfulness").style.whiteSpace = "pre-wrap";
  } else {
    $("faithfulness").textContent = result && result.faithfulness_error ? `Could not run: ${result.faithfulness_error}` : "Not run.";
  }

  const pending = job.review_status === "PENDING_REVIEW";
  show("review", pending);
  $("decision-msg").textContent = job.decision
    ? `Decision: ${job.decision.decision} by ${job.decision.decided_by_email || job.decision.decided_by} — ${job.decision.comment}` : "";
}

// ---------------------------------------------------------------- decide + audit
async function decide(kind) {
  const comment = $("comment").value.trim();
  if (!comment) { say("decision-msg", "Please write a comment first.", true); return; }
  try {
    const { body: current } = await api(`/investigations/${currentJob}`);
    await api(`/investigations/${currentJob}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision: kind, comment, result_hash: current.result.result_hash }),
    });
    watch(currentJob);
  } catch (error) { say("decision-msg", error.message, true); }
}
$("approve-btn").onclick = () => decide("approve");
$("escalate-btn").onclick = () => decide("escalate");
$("reject-btn").onclick = () => decide("reject");

async function loadAudit(jobId) {
  const { body } = await api(`/investigations/${jobId}/audit`);
  const list = document.createElement("ul");
  for (const e of body.events) {
    const li = document.createElement("li");
    li.textContent = `#${e.seq} ${e.event_type} by ${e.actor} at ${e.at}`;
    list.append(li);
  }
  const verdict = document.createElement("p");
  verdict.textContent = body.chain_ok ? "Audit chain verified." : `AUDIT CHAIN BROKEN: ${body.chain_problem}`;
  verdict.className = body.chain_ok ? "muted" : "err";
  $("audit").replaceChildren(list, verdict);
}
