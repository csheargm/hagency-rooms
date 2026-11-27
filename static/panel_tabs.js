// Tabs in the Hagency panel (panel.py serves this as /panel-tabs.js): Room · My agents · Cards. Owner request:
// docked above the chat the panel was one long page. Each tab is one of panel.html's sections ([data-tab]);
// the last one a viewer chose is remembered in this browser. Without this script (a proxy that doesn't
// route it) the panel stays one page, as before. Keyboard: the WAI-ARIA tabs pattern (arrows, Home, End).
const PANEL_TABS = [{id: "room", label: "Room"}, {id: "agents", label: "My agents"}, {id: "cards", label: "Cards"}];
const TAB_KEY = "hagency-panel-tab";
let tabChosen = "", tabsShown = [];      // tabChosen: clicked here, or remembered; kept while its tab is away (GPT review)
function savedTab() {
  try { return localStorage.getItem(TAB_KEY) || ""; } catch (e) { return ""; }   // storage blocked: the first tab
}
function saveTab(id) {
  try { localStorage.setItem(TAB_KEY, id); } catch (e) { /* storage blocked: remembered for this page only */ }
}
// Which tabs a view has: My Hagency (personal) and a room without cards have fewer; null (signed out): none.
// Cards only with its script (GPT review: a blank tab otherwise).
function tabsOf(s) {
  if (!s) return [];
  if (s.personal) return ["agents"];
  return PANEL_TABS.map(t => t.id).filter(id => id !== "cards" || (!!s.cards && typeof showCards === "function"));
}
// No choice yet: a newcomer with no agent starts on My agents, where "Get my agent" is (GPT review).
function pickTab(shown, wanted, s) {
  if (shown.includes(wanted)) return wanted;
  return !wanted && s && s.first_pick && shown.includes("agents") ? "agents" : shown[0] || "";
}
// panel.html's show() calls this on every view; it draws the tab bar only when the tabs change.
function tabsFor(s) {
  if (typeof document === "undefined" || !document.body || !document.body.classList) return;
  const shown = tabsOf(s), bar = document.getElementById("tabs");
  if (!bar) return;
  document.body.classList.toggle("tabbed", shown.length > 0);
  if (shown.join() !== tabsShown.join()) {
    tabsShown = shown;
    bar.innerHTML = PANEL_TABS.filter(t => shown.includes(t.id)).map(t => `<button role="tab" id="tab-${t.id}"
      data-tab-btn="${t.id}" aria-controls="pane-${t.id}">${t.label}</button>`).join("");
  }
  bar.hidden = shown.length < 2;              // one tab only (My Hagency): no bar
  showTab(pickTab(shown, tabChosen || savedTab(), s));
}
function selectTab(id) {                     // a person's choice: remembered
  tabChosen = id;
  saveTab(id);
  showTab(id);
}
function showTab(id) {
  const tabbed = tabsShown.length > 1;       // panes are tab panels only under a tab bar (GPT review)
  for (const pane of document.querySelectorAll("[data-tab]")) {   // signed out: every section, as before tabs
    pane.hidden = tabsShown.length > 0 && pane.dataset.tab !== id;
    for (const [k, v] of [["role", "tabpanel"], ["aria-labelledby", "tab-" + pane.dataset.tab], ["tabindex", "0"]])
      tabbed ? pane.setAttribute(k, v) : pane.removeAttribute(k);
  }
  for (const b of document.querySelectorAll("[data-tab-btn]")) {
    const on = b.dataset.tabBtn === id;
    b.setAttribute("aria-selected", String(on));
    b.tabIndex = on ? 0 : -1;
  }
}
const TAB_KEYS = {ArrowRight: 1, ArrowLeft: -1, Home: "first", End: "last"};
function tabKey(e) {
  const b = e.target.closest && e.target.closest("[data-tab-btn]"), step = TAB_KEYS[e.key];
  if (!b || step === undefined) return;
  e.preventDefault();
  const all = Array.from(document.querySelectorAll("[data-tab-btn]")), i = all.indexOf(b);
  const next = step === "first" ? all[0] : step === "last" ? all[all.length - 1] : all[(i + step + all.length) % all.length];
  selectTab(next.dataset.tabBtn);
  next.focus();
}
if (typeof document !== "undefined" && document.addEventListener) {
  document.addEventListener("click", e => {
    const b = e.target.closest && e.target.closest("[data-tab-btn]");
    if (b) selectTab(b.dataset.tabBtn);
  });
  document.addEventListener("keydown", tabKey);
}
