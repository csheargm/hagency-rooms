// "Your cards" (server/pm_cards.py, ADR 0019): views of this room that your own agent makes for you.
// Each card runs in a sandboxed frame (scripts only: no panel session, storage, forms or popups; network blocked
// by its policy, WebRTC only where browsers support blocking it: ADR 0019)
// and gets the room's data by message. Frames are kept across the panel's refreshes: one is redrawn only
// when its card changed, so a card never flickers.
(function () {
  const H_MIN = 60, H_MAX = 900, H_START = 140, ARM_MS = 4000;
  const frames = {};                         // card id → {v, box, frame}
  const root = () => document.getElementById("cards");
  // The controls are drawn once and updated in place: what someone is typing is never wiped (review).
  const SKELETON = `<section class="card"><h2>Your cards</h2><div class="field" id="cardrow"><input id="cardask"
    maxlength="300" placeholder="e.g. who built what this week, as a chart"><button class="primary" data-card="make">Make card</button></div>
    <div class="hint" id="cardhint"></div><div class="hint" id="cardbusy"></div><div class="hint" id="cardsays"></div></section>`;
  let says = "";                             // the last answer to a click here, shown in this card (not at the top)
  function controls(c) {
    const $ = id => document.getElementById(id), full = c.items.length >= c.max, blocked = c.unable || (!c.agent_here && NO_AGENT);
    $("cardrow").style.display = blocked || full ? "none" : "";
    $("cardhint").textContent = blocked || (full ? `You have ${c.max} cards here. Remove one to make another.`
      : c.items.length ? "" : "Describe a view of this project. Your agent builds it; only you see it here.");
    $("cardbusy").textContent = c.making ? (c.changing ? "⏳ Your agent is changing your card. This takes a minute or two."
      : `⏳ Your agent is making “${c.making}”. This takes a minute or two.`) : "";
    $("cardsays").textContent = says || (c.error ? "⚠️ " + c.error : "");   // the latest click's answer first
    $("cardsays").style.color = !says && c.error ? "var(--warn)" : "";
    document.querySelector('[data-card="make"]').disabled = !!c.making;
  }
  const NO_AGENT = "Your agent makes your cards. Bring it into this room first (My agents).";
  function box(card) {
    const el = document.createElement("section");
    el.className = "card"; el.dataset.cardId = card.id;
    el.innerHTML = `<div class="set" style="padding-top:0"><h2 style="margin:0"></h2><span>
      <button data-card="change">Change</button> <button data-card="remove">Remove</button></span></div>
      <div class="field" data-change style="display:none"><input maxlength="300" placeholder="What should change?">
      <button class="primary" data-card="change-send">Change it</button></div>
      <div class="hint" data-stopped style="display:none">This card tried to open another page, so it was stopped. Change or remove it.</div>
      <iframe sandbox="allow-scripts" referrerpolicy="no-referrer" title="" style="width:100%;border:0;height:${H_START}px;display:block"></iframe>`;
    return el;
  }
  async function load(id, entry, v) {        // the card's document (pm_cards.act card_html); a failure retries next refresh
    entry.loading = v;
    const r = await post("/console/action", {room, action: "card_html", value: id, agent: ""});
    if (entry.loading === v) entry.loading = "";
    if (!r.doc || frames[id] !== entry || entry.want !== v) return;
    entry.loads = 0; entry.loaded = v; entry.frame.srcdoc = r.doc;
    entry.frame.style.display = "block"; entry.box.querySelector("[data-stopped]").style.display = "none";
  }
  let data = null;
  function send(entry) {                     // only to the card's own document (a frame that navigated gets nothing)
    if (data && entry.loads === 1 && entry.frame.contentWindow) entry.frame.contentWindow.postMessage({hagency: data}, "*");
  }
  function loaded(entry) {                   // one load per srcdoc: a second means the card left its document
    if (++entry.loads === 1) return send(entry);
    if (entry.frame.style.display === "none") return;
    entry.frame.style.display = "none"; entry.frame.srcdoc = "";   // blank, until the card is changed
    entry.box.querySelector("[data-stopped]").style.display = "";
  }
  window.showCards = function (s) {
    const el = root(), c = s && s.cards;
    if (!el) return;
    if (!c) { el.innerHTML = ""; for (const k in frames) delete frames[k]; says = ""; return; }
    data = Object.assign({}, c.data, {mode: s.mode});
    if (!el.querySelector("#cardsctl")) el.innerHTML = `<div id="cardsctl">${SKELETON}</div><div id="cardlist" class="stack"></div>`;
    controls(c);
    const list = el.querySelector("#cardlist"), seen = new Set();
    for (const card of c.items) {
      seen.add(card.id);
      let entry = frames[card.id];
      if (!entry) {
        const b = box(card);
        entry = frames[card.id] = {want: "", loaded: "", loading: "", loads: 0, box: b, frame: b.querySelector("iframe")};
        entry.frame.addEventListener("load", () => loaded(entry));
      }
      if (entry.box.parentNode !== list) list.appendChild(entry.box);   // once: moving a frame would reload it
      entry.box.querySelector("h2").textContent = card.title;
      entry.box.querySelector("h2").title = "You asked: " + card.request;
      entry.frame.title = card.title;
      for (const b of entry.box.querySelectorAll('[data-card="change"], [data-card="change-send"]')) b.disabled = !!c.making;
      entry.want = card.v;
      if (entry.loaded !== card.v && entry.loading !== card.v) load(card.id, entry, card.v);
      else send(entry);
    }
    for (const id of Object.keys(frames)) if (!seen.has(id)) { frames[id].box.remove(); delete frames[id]; }
  };
  window.addEventListener("message", ev => {   // a card tells its height; nothing else from a card is read
    const entry = Object.values(frames).find(f => f.frame.contentWindow === ev.source);
    const h = entry && ev.data && Number(ev.data.cardHeight);
    if (h) entry.frame.style.height = Math.max(H_MIN, Math.min(H_MAX, Math.ceil(h))) + "px";
  });
  async function cardAct(action, value) {
    says = "…"; document.getElementById("cardsays").textContent = says;
    const r = await post("/console/action", {room, action, value, agent: ""});
    says = r.result === "ok" ? (action === "card_remove" ? "Removed." : "") : (r.result || r.error);
    refresh();
    return r.result === "ok";
  }
  document.addEventListener("click", async e => {
    const b = e.target.closest && e.target.closest("button[data-card]");
    if (!b) return;
    const cardBox = b.closest("[data-card-id]"), id = cardBox && cardBox.dataset.cardId;
    if (b.dataset.card === "make") {
      const input = document.getElementById("cardask"), text = input.value.trim();
      if (!text) return void input.focus();
      if (await cardAct("card_make", text)) input.value = "";
    } else if (b.dataset.card === "change") {
      const f = cardBox.querySelector("[data-change]"), open = f.style.display === "none";
      f.style.display = open ? "" : "none";
      if (open) f.querySelector("input").focus();
    } else if (b.dataset.card === "change-send") {
      const input = cardBox.querySelector("[data-change] input"), text = input.value.trim();
      if (!text) return void input.focus();
      if (await cardAct("card_change", id + "\n" + text)) { input.value = ""; cardBox.querySelector("[data-change]").style.display = "none"; }
    } else if (b.dataset.card === "remove") {
      if (b.dataset.armed !== "1") {         // two clicks: a card is gone for good
        b.dataset.armed = "1"; b.textContent = "Confirm";   // same width: a longer label wrapped and moved the button (live test)
        setTimeout(() => { b.dataset.armed = ""; b.textContent = "Remove"; }, ARM_MS);
        return;
      }
      cardAct("card_remove", id);
    }
  });
  document.addEventListener("keydown", e => {   // Enter in a card's box clicks its button
    if (e.key !== "Enter" || e.isComposing || e.keyCode === 229 || !e.target.closest || !e.target.closest("#cards") || e.target.tagName !== "INPUT") return;
    const btn = e.target.closest(".field").querySelector("button");
    if (btn && !btn.disabled) btn.click();
  });
})();
