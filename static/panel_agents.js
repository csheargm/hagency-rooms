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
  return `<span class="chip">${word}</span>${a.stopped ? ' <span class="chip">Stopped</span>' : ""} ${codeChip(a)} ${stopButton(a)} ${disconnect}
    ${fresh} ${removeButton(a)}`;
}
// Versions (console_update.py, ADR 0020): its code against this server's. Update: it updates itself on its
// computer; one whose code predates that gets "Update my agent" (its Connect page, run there once).
function codeChip(a) {
  if (!a.remote || !(a.online === true || a.connected)) return "";
  const chip = (word, tip) => `<span class="chip"${tip ? ` title="${esc(tip)}"` : ""}>${word}</span>`;
  if (a.code === "current") return chip("Up to date", a.code_version);
  if (a.code === "own") return chip("Your own version", a.code_version);
  if (a.code !== "behind" && !a.code_fallback) return "";
  const button = a.online !== true || a.code_updating ? ""
    : a.code_fallback ? `<button data-a="update" data-v="agents" data-g="${esc(a.name)}">Update my agent</button>`
    : `<button data-a="selfupdate" data-v="" data-g="${esc(a.name)}">Update</button>`;
  return chip("Update available", a.code_version) + " " + button;
}
function codeHint(a) {
  if (!a.remote || !(a.online === true || a.connected)) return "";
  const says = a.code_says || (a.code_fallback && a.online === true ? "This agent's code is from before one-click updates. " +
    "Click Update my agent and run the command it sends on its computer, once. After that, Update takes one click." : "");
  const ver = a.code_version ? `Version ${esc(a.code_version)}` : "";
  return says || ver ? `<div class="hint">${[esc(says), ver].filter(Boolean).join(" · ")}</div>` : "";
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
  return `<li><div class="field"><input id="rename-${esc(a.name)}" maxlength="40" placeholder="Name people see"
    value="${esc(a.display || "")}"><button data-a="rename" data-v="" data-g="${esc(a.name)}">Rename</button></div></li>`;
}
function agentRow(a, version, several, personal) {
  const called = a.display || (several ? a.label : "");     // the name its person gave it, then its account
  const title = (called ? `<b>${esc(called)}</b> <span class="muted">· ${esc(a.name)}</span>` : `<b>${esc(a.name)}</b>`)
    + (a.runs ? ` <span class="chip">${esc(a.runs)}</span>` : "");   // the AI it runs (pm_kinds.describe)
  const where = a.here ? ' <span class="chip">in this room</span>'
    : a.invited ? ' <span class="chip">invited to this room</span>' : "";
  return `<li><div>${title}${where}${agentHint(a, personal)}${stopHint(a)}${codeHint(a)}</div>
    <div>${agentStatus(a)}</div></li>${renameRow(a)}
    <li><div class="hint">Comes along when I join a project room${a.first ? "" : " (or invite it yourself)"}</div>${agentSeg("follow", a.name, ["always", "ask", "never"], a.follow)}</li>
    ${a.first && version ? `<li><div class="hint">Code it runs: your own version (${esc(version)})</div>
      <button data-a="goback" data-v="">Go back to official</button></li>` : ""}`;
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
  const add = s.first_pick ? firstPick(s) : s.can_add ? `<div class="set"><div class="field"><input id="newagent" maxlength="9" placeholder="name, e.g. codex">
    ${kindPicker(s.kinds)}<button data-a="add" data-v="">Add an agent</button></div>
    <div class="hint">@pm sends its Connect page ${s.personal ? "here, in this chat" : "in " + chatLink("your private chat")}. ${KEY_NOTE}</div></div>` : "";
  return `<section class="card"><h2>My agents · ${s.agents.length}</h2><div class="hint">The same in every room.</div>
    <ul class="agents">${s.agents.map(a => agentRow(a, s.version, s.agents.length > 1, s.personal)).join("")}</ul>${add}</section>`;
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
