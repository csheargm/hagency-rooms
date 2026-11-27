// "My agents" in the Hagency panel and console (panel.py serves this as /panel-agents.js).
// A person may have several agents (ADR 0005): each has its own Online/Offline, Stop/Start, Disconnect, Remove and
// "comes along" setting. Every value shown comes from /console/state and is escaped here;
// every change goes through @pm's own checks (console.act).
function agentSeg(name, agent, values, current) {
  return `<span class="seg">` + values.map(v => `<button data-a="${esc(name)}" data-v="${esc(v)}" data-g="${esc(agent)}"
    ${v === current ? 'class=on' : ''}>${esc(cap(v))}</button>`).join("") + `</span>`;
}
function agentStatus(a) {
  if (!a.remote) return `<span class="chip">Runs on this server</span>`;
  const word = a.online === true ? "Online" : !a.connected ? "Not connected yet" : "Offline";
  // Screen test: one that never connected had a Disconnect button (nothing to disconnect); Remove and Rename stay.
  const disconnect = a.online === true || a.connected
    ? `<button data-a="disconnect" data-v="" data-g="${esc(a.name)}">Disconnect</button>` : "";
  // Screen test: a never-connected agent's only way to a fresh code was typing a command in the chat.
  const fresh = a.online !== true && !a.connected
    ? `<button data-a="code" data-v="" data-g="${esc(a.name)}">Get a fresh code</button>` : "";
  // Its version and GitHub buttons are on its summary line (agentLine); "Sign in again" here, once signed in.
  return `<span class="chip">${word}</span>${a.stopped ? ' <span class="chip">Stopped</span>' : ""} ${a.gh === "signed_in" ? ghChip(a) : ""}
    ${stopButton(a)} ${disconnect} ${fresh} ${removeButton(a)}`;
}
// Versions (console_update.py, ADR 0020): its code against this server's. Update: it updates itself on its
// computer; one whose code predates that gets "Update my agent" (its Connect page, run there once).
// The commit's time, to the minute, in the viewer's own time zone: "2026-11-25 14:32 PST". The server
// sends it in UTC (version.iso); "" when it isn't a time, and the server's UTC words stay.
function localTime(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "";
  let p = {};
  try {
    new Intl.DateTimeFormat("en-US", {year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      hourCycle: "h23", timeZoneName: "short"}).formatToParts(d).forEach(x => { p[x.type] = x.value; });
  } catch (e) { return ""; }   // a browser without Intl's formatToParts (GPT review): the server's UTC words stay
  return p.year && p.timeZoneName ? `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute} ${p.timeZoneName}` : "";
}
// The footer's commit time (panel.FOOTER, <time id="vertime">) in the viewer's zone; its UTC words stay otherwise.
function localizeFooter(t) {
  const local = t ? localTime(t.dateTime) : "";
  if (local) t.textContent = local;
}
const codeVersion = a => a.code_name && localTime(a.code_time) ? `${a.code_name} · ${localTime(a.code_time)}` : a.code_version;
function codeChip(a) {
  if (!a.remote || !(a.online === true || a.connected)) return "";
  const chip = (word, tip) => `<span class="chip"${tip ? ` title="${esc(tip)}"` : ""}>${word}</span>`;
  if (a.code === "current") return chip("Up to date", codeVersion(a));
  if (a.code === "own") return chip("Your own version", codeVersion(a));
  if (a.code_auto && a.code === "behind") {   // Auto-update (ADR 0024): no Update button; Retry update after a failure
    const fix = a.online !== true ? "" : a.code_fallback   // edited, not official: its Connect page, as without Auto-update
      ? ` <button data-a="update" data-v="agents" data-g="${esc(a.name)}">Update my agent</button>`
      : a.code_retry ? ` <button data-a="selfupdate" data-v="" data-g="${esc(a.name)}">Retry update</button>` : "";
    return chip(a.code_updating ? "Updating…" : "Update available", codeVersion(a)) + fix;
  }
  if (a.code !== "behind" && !a.code_fallback) return "";
  const button = a.online !== true || a.code_updating ? ""
    : a.code_fallback ? `<button data-a="update" data-v="agents" data-g="${esc(a.name)}">Update my agent</button>`
    : `<button data-a="selfupdate" data-v="" data-g="${esc(a.name)}">Update</button>`;
  return chip("Update available", codeVersion(a)) + " " + button;
}
function codeHint(a) {
  if (!a.remote || !(a.online === true || a.connected)) return "";
  const says = a.code_says;               // console_update.status: the words, one place
  const ver = codeVersion(a) ? `Version ${esc(codeVersion(a))}${a.code_auto && a.code === "current" ? " · updates automatically" : ""}` : "";
  return says || ver ? `<div class="hint">${[esc(says), ver].filter(Boolean).join(" · ")}</div>` : "";
}
// Sign in to GitHub (console_github.py, ADR 0021): the agent's own GitHub account, for issues and pull requests.
// GitHub's code shows only here (My agents is its owner's alone) and in their chat with Product Manager.
const ghSeen = a => a.remote && (a.online === true || a.connected) && !!a.gh;
function ghChip(a) {
  if (!ghSeen(a)) return "";
  const can = a.gh !== "old" && a.online === true && !a.gh_busy;
  if (a.gh === "signed_in")             // its token may be revoked since: a quiet way to sign in again (GPT re-check)
    return `<span class="chip">GitHub: ${a.gh_login ? "@" + esc(a.gh_login) : "signed in"} ✓</span>` +
      (can ? ` <button data-a="github_signin" data-v="" data-g="${esc(a.name)}" class="quiet">Sign in again</button>` : "");
  return can ? `<button data-a="github_signin" data-v="" data-g="${esc(a.name)}">Sign in to GitHub</button>` : "";
}
// The code with Copy, as on the GitHub card (panel_github.js: copyCode, its click handler, copiedUntil).
const ghFieldId = a => "ghcode-" + a.name;
function ghCodeField(a) {
  const copied = typeof copiedUntil !== "undefined" && Date.now() < copiedUntil;
  return `<input id="${esc(ghFieldId(a))}" class="code" readonly size="10" value="${esc(a.gh_code)}">
    <button data-copy="${esc(a.gh_code)}" data-field="${esc(ghFieldId(a))}">${copied ? "Copied ✓" : "Copy"}</button>`;
}
// panel.html's redraw guard: a focused code field stays while its agent still shows that code.
const ghCodeKept = (typing, s, value) => !!typing && (s.agents || []).some(a => ghFieldId(a) === typing && a.gh_code === value);
function ghHint(a, personal) {
  if (!ghSeen(a) || !(a.gh_says || a.gh_code)) return "";
  const where = personal === true ? "this chat" : chatLink("your chat with Product Manager");
  const code = a.gh_code ? `Open <a href="${esc(a.gh_url)}" target="_blank" rel="noopener noreferrer">${esc(a.gh_url)}</a>
    and enter ${ghCodeField(a)}, signed in as the GitHub account this agent should use. It works
    for 15 minutes, and it's also in ${where}.` : esc(a.gh_says);
  const cli = a.gh_cli && a.gh !== "old" && !a.gh_code ? `<br>Or in a terminal on its computer, in <code>~/.hagency/app</code>:
    <code>${esc(a.gh_cli)}</code>` : "";       // plain, not a <details>: the panel's redraw would close it
  return `<div class="hint">${code}${cli}</div>`;
}
// Stop / Start (pm_stop.py): it stays signed in on its computer and ignores messages until Start. Only for
// one that has connected; Hermes answers through its own gateways, so it is stopped on its computer.
function stopButton(a) {
  if (a.hermes || !(a.online === true || a.connected || a.stopped)) return "";
  return a.stopped ? `<button data-a="start" data-v="" data-g="${esc(a.name)}">Start</button>`
    : `<button data-a="stop" data-v="" data-g="${esc(a.name)}">Stop</button>`;
}
function stopHint(a) {
  if (a.stopped) return '<div class="hint">Stopped: it ignores messages and tasks until you click Start.</div>';
  return a.hermes && a.online === true ? '<div class="hint">To stop a Hermes agent, run <code>~/.hagency/app/.venv/bin/python ~/.hagency/app/bin/connect.py --stop</code> on its computer.</div>' : "";
}
// Remove is for good, so it asks twice in the page (Element's widget frame may block confirm()).
let removeArmed = {agent: "", until: 0};
function removeButton(a) {
  const armed = removeArmed.agent === a.name && Date.now() < removeArmed.until;
  return `<button data-a="remove" data-v="${armed ? "sure" : ""}" data-g="${esc(a.name)}">${
    armed ? "Click again to remove it for good" : "Remove"}</button>`;
}
function confirmed(b) {                // panel.py's click handler: false = this click only armed Remove
  if (b.dataset.a !== "remove") return true;
  if (b.dataset.v === "sure" && removeArmed.agent === b.dataset.g && Date.now() < removeArmed.until) return true;
  removeArmed = {agent: b.dataset.g, until: Date.now() + 8000};
  b.dataset.v = "sure";
  b.textContent = "Click again to remove it for good";
  return false;
}
// "your chat with Product Manager" opens it (panel.html's pmChatLink; without the page: plain words).
const chatLink = text => typeof pmChatLink === "function" ? pmChatLink(text) : esc(text);
// A screen test found newcomer jargon here: the label "main" for their only agent, "run the connect
// line again" for one that never connected, and "the official version" nobody had changed.
// In My Hagency (personal: @pm's private chat itself) it says "here", never "open your chat" (screen test).
function agentHint(a, personal) {
  if (!a.remote || a.online === true) return "";
  personal = personal === true;            // never an index (rows.map(agentHint))
  if (a.connected) return '<div class="hint">Run the connect line again on its computer; @pm sends its Connect page with a fresh code (🔁).</div>';
  // A code lasts 30 minutes; "Get a fresh code" (console_agents.fresh_code) sends a new Connect page.
  // Screen test: this hint told people to type "resend me the code" or "add another agent called pi".
  const where = personal ? "here, in this chat" : `in ${chatLink("your chat with Product Manager")}`;
  return `<div class="hint">Click Get a fresh code to connect it: its Connect page comes ${where}.</div>`;
}
// Rename: only the name people see (pm_rename.py); its Matrix ID stays. Plain text, up to 40 characters.
function renameRow(a) {
  return `<div class="field"><input id="rename-${esc(a.name)}" maxlength="40" placeholder="Name people see"
    value="${esc(a.display || "")}"><button data-a="rename" data-v="" data-g="${esc(a.name)}">Rename</button></div>`;
}
// One line per agent (owner request: My agents took the whole panel); a click on it shows the rest, one
// agent at a time. The panel redraws every few seconds, so which one is open lives here.
let agentOpen = null;                    // null: not chosen yet (one waiting to connect opens); "": all closed
let agentAuto = "";                      // the one opened for that, this view (agentCards)
function agentRow(a, version, several, personal) {
  const called = a.display || (several ? a.label : "");     // the name its person gave it, then its account
  const where = a.here ? ' <span class="chip">in this room</span>'
    : a.invited ? ' <span class="chip">invited to this room</span>' : "";
  const open = agentOpen === null ? agentAuto === a.name : agentOpen === a.name, id = "adet-" + a.name;
  return `<li class="arow">${agentLine(a, called, open, id)}${ghHint(a, personal)}
    <div class="adet" id="${esc(id)}"${open ? "" : " hidden"}><div>${called ? `<b>${esc(called)}</b> <span class="muted">· ${esc(a.name)}</span>`
      : `<b>${esc(a.name)}</b>`}${where}</div>${agentHint(a, personal)}${stopHint(a)}${codeHint(a)}<div>${agentStatus(a)}</div>${renameRow(a)}
    <div><div class="hint">Comes along when I join a project room${a.first ? "" : " (or invite it yourself)"}</div>${agentSeg("follow", a.name, ["always", "ask", "never"], a.follow)}</div>
    ${a.first && version ? `<div><div class="hint">Code it runs: your own version (${esc(version)})</div>
      <button data-a="goback" data-v="">Go back to official</button></div>` : ""}</div></li>`;
}
// The summary line: status dot, name, the AI it runs (pm_kinds.describe), its version, GitHub. Its buttons act;
// a click anywhere else on it opens or closes the agent.
function agentLine(a, called, open, id) {
  const on = !a.remote || a.online === true, fresh = a.remote && a.online !== true && !a.connected;
  const word = !a.remote ? "Runs on this server" : a.stopped ? "Stopped" : on ? "Online" : fresh ? "Not connected yet" : "Offline";
  const dot = a.stopped ? "stop" : on ? "on" : fresh ? "new" : "off";
  return `<div class="aline" data-open="${esc(a.name)}"><button class="ahead" aria-expanded="${open}" aria-controls="${esc(id)}"
    data-open="${esc(a.name)}"><span class="dot ${dot}"></span><b>${esc(called || a.name)}</b></button>${
    a.runs ? `<span class="chip">${esc(a.runs)}</span>` : ""}<span class="muted small">${word}</span>${lineVersion(a)}${lineGithub(a)}</div>`;
}
function lineVersion(a) {                // "v1.2.0 · updates automatically", or Updating… / Retry update / Update
  const chip = codeChip(a), short = String(a.code_name || a.code_version || "").split(" · ")[0];
  if (chip && a.code === "current" && a.code_auto) return `<span class="muted small">${esc(short)} · updates automatically</span>`;
  return chip ? `${short ? `<span class="muted small">${esc(short)}</span>` : ""}${chip}` : "";
}
const lineGithub = a => a.gh === "signed_in" && ghSeen(a)        // its @login and "Sign in again": in its details
  ? `<span class="chip">GitHub ✓</span>` : ghChip(a);
// A click opens that agent and closes the one open before, at once (not at the next redraw).
function toggleAgent(name) {
  const now = agentOpen === null ? agentAuto : agentOpen;
  agentOpen = now === name ? "" : name;
  for (const d of document.querySelectorAll(".adet")) d.hidden = d.id !== "adet-" + agentOpen;
  for (const b of document.querySelectorAll(".ahead")) b.setAttribute("aria-expanded", String(b.dataset.open === agentOpen));
}
let addOpen = false;                     // "+ Add agent" shows its form (owner request: it was always there)
function toggleAdd(b) {
  addOpen = !addOpen;
  b.setAttribute("aria-expanded", String(addOpen));
  document.getElementById("addform").hidden = !addOpen;
  if (addOpen) document.getElementById("newagent").focus();
}
let rowsListened = false;                // the click listener, added once, at the first draw (as showNeeds)
function listenRows() {
  if (rowsListened || typeof document === "undefined" || !document.addEventListener) return;
  rowsListened = true;
  document.addEventListener("click", e => {
    const t = e.target && e.target.closest ? e.target : null;
    if (!t || t.closest("button[data-a], button[data-copy], input, select, a")) return;   // the line's own buttons act
    const line = t.closest("[data-open]"), add = t.closest("[data-addtoggle]");
    if (line) toggleAgent(line.dataset.open);
    if (add) toggleAdd(add);
  });
}
// The picker for their FIRST agent too (console_agents.first_pick: none yet, or none has connected; screen
// test: a newcomer could only get Codex or Claude Code). No name: the first agent has its own.
function firstPick(s) {
  const again = s.agents.length > 0;
  return `<div class="set"><div class="field">${kindPicker(s.kinds)}<button data-a="add" data-v="">${
    again ? "Use this AI" : "Get my agent"}</button></div>
    <div class="hint">${again ? "Not connected yet, so you can still choose the AI it runs" : "Choose the AI your agent runs"}:
    @pm sends its Connect page ${s.personal ? "here, in this chat" : "in " + chatLink("your private chat")}. ${KEY_NOTE}</div></div>`;
}
const KEY_NOTE = "A remote model's API key is asked for on your computer when it connects, never here.";
function agentCards(s) {
  if (!s.agents || (!s.agents.length && !s.can_add && !s.first_pick)) return "";   // none, and none to add
  listenRows();
  const add = s.first_pick ? firstPick(s) : s.can_add ? `<button data-addtoggle="1" aria-expanded="${addOpen}" aria-controls="addform">+ Add agent</button>
    <div class="set" id="addform"${addOpen ? "" : " hidden"}><div class="field"><input id="newagent" maxlength="9" placeholder="name, e.g. codex">
    ${kindPicker(s.kinds)}<button data-a="add" data-v="">Add an agent</button></div>
    <div class="hint">@pm sends its Connect page ${s.personal ? "here, in this chat" : "in " + chatLink("your private chat")}. ${KEY_NOTE}</div></div>` : "";
  const waiting = s.agents.find(a => a.remote && a.online !== true && !a.connected);   // opens until one is clicked
  agentAuto = waiting ? waiting.name : "";
  return `<section class="card"><h2>My agents · ${s.agents.length}</h2><div class="hint">The same in every room. Click an agent for its settings.</div>
    <ul class="agents">${s.agents.map(a => agentRow(a, s.version, s.agents.length > 1, s.personal)).join("")}</ul>${autoSwitch(s)}${add}</section>`;
}
// The shared trial agent (pm_trial.py, ADR 0025): only while it runs and you have no agent of your own (the server
// sends s.trial only then, in My Hagency). Shown in My agents, under your agents (panel.html: personal).
function trialCard(t) {
  if (!t) return "";
  const what = t.on ? `Its chat is <b>Trial agent</b> (accept the invite under Invites if you haven't). ${esc(t.left)} of ${esc(t.per_day)} messages left today.`
    : `No agent yet? Talk to a shared, limited AI (${esc(t.per_day)} messages a day): questions, ideas, reading a pull request. It can't run code, build or merge.`;
  const pick = t.on ? `<div class="set"><div>Model: <b>${esc(t.model)}</b></div><span class="field"><select id="trialmodel">${
    (t.models || []).map(m => `<option ${m === t.model ? "selected" : ""}>${esc(m)}</option>`).join("")}</select>
    <button data-a="trial_model">Use</button></span></div>` : "";
  return `<section class="card"><h2>Trial agent (shared, limited)</h2><div class="set"><div>${what}<div class="hint">What you write to it goes to Ollama Cloud.</div></div>
    <button class="${t.on ? "" : "primary"}" data-a="trial" data-v="${t.on ? "again" : "start"}">${t.on ? "Open its chat again" : "Try it"}</button></div>${pick}</section>`;
}
// Auto-update (console_auto_update.py, ADR 0024): one switch for all of a person's agents on their computers, on by
// default. Small (owner request); what it does is in its tooltip.
function autoSwitch(s) {
  const one = (s.agents || []).find(a => a.remote);
  if (!one) return "";
  return `<div class="set small" title="On: your agents update themselves to the latest Hagency version when they have nothing running,
    and Product Manager tells you in your chat with it. Off: you click Update on each agent."><span>Auto-update, all my agents</span>
    ${agentSeg("autoupdate", "", ["on", "off"], one.auto_update ? "on" : "off")}</div>`;
}
// The AI a new agent runs: s.kinds is agent_kinds.kinds() (the one list: kind, name, needs, windows);
// "" = the one its computer finds, as before. A kind that can't run on Windows says so, and is greyed
// out when this browser runs on Windows. Choosing one shows what it needs. Ollama: the model is picked
// on that computer (or named here); remote: base URL and model here.
const onWindows = () => typeof navigator !== "undefined" && /Win/i.test(navigator.platform || navigator.userAgent || "");
function kindOption(k) {
  const needs = `Needs on your computer: ${(k.needs || []).join("; ")}.`;
  return `<option value="${esc(k.kind)}" data-needs="${esc(needs)}"${!k.windows && onWindows() ? " disabled" : ""}>`
    + `${esc(k.name)}${k.windows ? "" : " (Mac/Linux only)"}</option>`;
}
let needsShown = false;                 // the change listener, added once, at the first draw
function showNeeds() {
  if (needsShown || typeof document === "undefined") return;
  needsShown = true;
  document.addEventListener("change", e => {   // the chosen kind's needs, under the picker
    const out = document.getElementById("newneeds"), sel = e.target;
    if (out && sel && sel.id === "newkind") out.textContent = (sel.selectedOptions[0] || {dataset: {}}).dataset.needs || "";
  });
}
function kindPicker(kinds) {
  showNeeds();
  return `<select id="newkind"><option value="" data-needs="">AI: the one on my computer</option>${(kinds || []).map(kindOption).join("")}</select>
    <input id="newmodel" maxlength="100" placeholder="model (Ollama: optional)">
    <input id="newbase" maxlength="200" placeholder="base URL (remote only), https://…">
    <div class="hint" id="newneeds"></div>`;
}
function addValue() {                    // panel.html's Add an agent click: what goes to console.act
  const v = id => (document.getElementById(id) || {value: ""}).value.trim();
  const name = v("newagent"), kind = v("newkind");
  // Always the picker's JSON (GPT review): "the one on my computer" for a first agent then clears an earlier choice.
  return JSON.stringify({name, agent_kind: kind, model: kind ? v("newmodel") : "", base_url: kind ? v("newbase") : ""});
}
