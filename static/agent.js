// Get your agent (server/agent_page.py, docs/adr/0015-one-click-home-steps.md): sign in (static/
// signin.js), choose the AI, and @pm gives a one-time code for this person's first agent. The page
// then opens the Connect page with it, exactly as @pm's message links there: the code is in the
// fragment, which no server sees. The only places this page sends anyone: our homeserver's SSO and
// this server's Connect page.
"use strict";
const CODE_RE = /^[A-Z0-9]{5}-[A-Z0-9]{5}$/;        // pairing.py's shape: nothing else gets in the address
const KIND_RE = /^[a-z]{2,16}$/;
const MODEL_KINDS = ["ollama", "remote"];          // agent_kinds.MODEL_KINDS: only these take a model here
const IDP_KEY = "hagency-agent-idp";
const SAYS = {offline: "We couldn't reach Hagency Rooms. Check your connection and try again.",
  wrong: "That username and password didn't work. Please try again.",
  refused: "We couldn't confirm your sign-in. Please try again.",
  not_signed_in: "Please sign in again.",
  busy: "Too many tries from here. Please wait a few minutes and try again.",
  failed: "Something went wrong. Please try again in a minute."};

/** The Connect page for this code (and kind), or "" if either isn't the shape @pm gives. */
function connectLink(code, kind) {
  if (!CODE_RE.test(String(code || ""))) return "";
  return `/connect/start#${code}` + (KIND_RE.test(String(kind || "")) ? `&kind=${kind}` : "");
}

/** The choice as the panel's picker sends it (pm_kinds.from_panel); "" kind: the one on their computer. */
function pickOf(kind, model, base) {
  const takes = MODEL_KINDS.includes(kind);
  return {name: "", agent_kind: kind || "", model: takes ? String(model || "").trim() : "",
    base_url: kind === "remote" ? String(base || "").trim() : ""};
}

/** What "Your agent" says once signed in (agent_page.status_of). */
function haveText(me) {
  const runs = me.runs ? ` (${me.runs})` : "";
  if (me.connected) return `Your agent ${me.agent} is connected already${runs}. To connect it on another computer, get a new command.`;
  if (!me.pick) return `Your agent is ${me.agent}${runs}. It keeps its AI.`;
  return `Your agent will be ${me.agent}.`;
}

// The page itself (browser only; SignIn is static/signin.js).
let session = null, kinds = [];   // @pm's page session (server/page_session.py), in memory only
const $ = id => document.getElementById(id);
const say = (text, ok) => { const s = $("status"); s.textContent = text || ""; s.hidden = !text; s.className = ok ? "status ok" : "status"; };
const signInAgain = text => { session = null; say(text); $("choose").hidden = true; $("signin").hidden = false; };
const why = r => r.data.error || SAYS[r.data.state] || (r.status ? SAYS.failed : SAYS.offline);

function fillKinds(picked) {
  const sel = $("kind");
  sel.replaceChildren(new Option("The one on my computer (Codex or Claude Code)", ""), ...kinds.map(k => new Option(k.name, k.kind)));
  sel.value = kinds.some(k => k.kind === picked.agent_kind) ? picked.agent_kind : "";
  $("model").value = picked.model || "";
  $("base").value = picked.base_url || "";
  showKind();
}

function showKind() {
  const kind = $("kind").value, k = kinds.find(x => x.kind === kind);
  $("needs").textContent = k ? "Your computer needs: " + k.needs.join("; ") + "." : "Your computer needs Codex or Claude Code, installed and signed in.";
  $("model-box").hidden = !MODEL_KINDS.includes(kind);
  $("model-hint").textContent = kind === "remote" ? "For example openai/gpt-4o." : "For example llama3.2. Leave it empty to choose on your computer.";
  $("base-box").hidden = kind !== "remote";
}

async function ask(path, body) {
  try { return await SignIn.post(path, {session, ...body}); } catch (e) { return {status: 0, data: {}}; }
}

async function signedIn(body) {
  $("signin").hidden = true; say("Signing you in…", true);
  const me = await SignIn.handOver(body, "Hagency: get your agent", "/connect/new/me");   // @pm's own page session comes back
  if (me.error) { say(SAYS[me.error]); $("signin").hidden = false; return; }
  session = typeof me.data.session === "string" ? me.data.session : null;
  if (me.status === 401) return signInAgain(SAYS.not_signed_in);
  if (me.data.state !== "ok") return me.data.state === "refused" ? say(why(me)) : signInAgain(why(me));   // else: try again
  say("");
  $("have").textContent = haveText(me.data);
  $("pick").hidden = !me.data.pick;
  fillKinds(me.data.picked || {});
  $("choose").hidden = false;
}

async function go() {
  $("go").disabled = true; say("Setting up your agent… This can take a few seconds.", true);
  const pick = $("pick").hidden ? null : pickOf($("kind").value, $("model").value, $("base").value);
  const r = await ask("/connect/new/code", {pick});
  $("go").disabled = false;
  if (r.status === 401) return signInAgain(SAYS.not_signed_in);
  const link = r.data.state === "ok" ? connectLink(r.data.code, r.data.kind) : "";
  if (!link) return say(why(r));
  location.assign(link);
}

function start() {
  try { kinds = JSON.parse($("kinds").textContent); } catch (e) { kinds = []; }   // no list: "the one on my computer" still works
  $("kind").addEventListener("change", showKind);
  $("go").addEventListener("click", go);
  SignIn.form($("password-box"), "Sign in", signedIn);
  SignIn.ssoButtons($("sso"), IDP_KEY);    // there for a retry too (GPT review)
  const back = SignIn.returned(IDP_KEY);   // Google or GitHub brought this tab back: finish that sign-in
  if (back) return signedIn(back.login);
  $("signin").hidden = false;
}

if (typeof module === "object" && module.exports) {   // tests/test_home_steps_js.py runs these under node
  module.exports = {connectLink, pickOf, haveText};
} else {
  document.addEventListener("DOMContentLoaded", start);
}
