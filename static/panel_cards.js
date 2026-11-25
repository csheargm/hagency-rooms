// "Your cards" (server/pm_cards.py, ADR 0019): views of this room that your own agent makes for you.
// Each card runs in a sandboxed frame (scripts only: no panel session, storage, forms, popups or network)
// and gets the room's data by message. Frames are kept across the panel's refreshes: one is redrawn only
// when its card changed, so a card never flickers.
(function () {
  const H_MIN = 60, H_MAX = 900, H_START = 140, ARM_MS = 4000;
  const frames = {};                         // card id → {v, box, frame}
  let ctlSig = "";
  const esc = t => String(t).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const root = () => document.getElementById("cards");
  function controls(c) {
    const busy = c.making ? `<div class="hint">⏳ Your agent is ${c.changing ? "changing a card" : "making"}: “${esc(c.making)}”…</div>` : "";
    const err = c.error ? `<div class="hint" style="color:var(--warn)">⚠️ ${esc(c.error)}</div>` : "";
    const full = c.items.length >= c.max;
    const ask = !c.agent_here ? `<div class="hint">Your agent makes your cards. Bring it into this room first (My agents).</div>`
      : full ? `<div class="hint">You have ${c.max} cards here. Remove one to make another.</div>`
      : `<div class="field"><input id="cardask" maxlength="300" placeholder="e.g. who built what this week, as a chart">
         <button class="primary" data-card="make" ${c.making ? "disabled" : ""}>Make card</button></div>
         ${c.items.length ? "" : `<div class="hint">Describe a view of this project. Your agent builds it here, just for you.</div>`}`;
    return `<section class="card"><h2>Your cards</h2>${ask}${busy}${err}</section>`;
  }
  function box(card) {
    const el = document.createElement("section");
    el.className = "card"; el.dataset.cardId = card.id;
    el.innerHTML = `<div class="set" style="padding-top:0"><h2 style="margin:0"></h2><span>
      <button data-card="change">Change</button> <button data-card="remove">Remove</button></span></div>
      <div class="field" data-change hidden><input maxlength="300" placeholder="What should change?">
      <button class="primary" data-card="change-send">Change it</button></div>
      <iframe sandbox="allow-scripts" referrerpolicy="no-referrer" title="" style="width:100%;border:0;height:${H_START}px;display:block"></iframe>`;
    return el;
  }
  async function load(id, entry) {           // the card's document (pm_cards.act card_html), then its data
    const r = await post("/console/action", {room, action: "card_html", value: id, agent: ""});
    if (r.doc && frames[id] === entry) entry.frame.srcdoc = r.doc;
  }
  let data = null;
  function send(entry) { if (data && entry.frame.contentWindow) entry.frame.contentWindow.postMessage({hagency: data}, "*"); }
  window.showCards = function (s) {
    const el = root(), c = s && s.cards;
    if (!el) return;
    if (!c) { el.innerHTML = ""; for (const k in frames) delete frames[k]; ctlSig = ""; return; }
    data = Object.assign({}, c.data, {mode: s.mode});
    if (!el.querySelector("#cardsctl")) el.innerHTML = '<div id="cardsctl"></div><div id="cardlist" class="stack"></div>';
    const typing = el.contains(document.activeElement) && document.activeElement.tagName === "INPUT";
    const sig = JSON.stringify([c.making, c.changing, c.error, c.agent_here, c.items.length, c.max]);
    if (sig !== ctlSig && !typing) { el.querySelector("#cardsctl").innerHTML = controls(c); ctlSig = sig; }
    const list = el.querySelector("#cardlist"), seen = new Set();
    for (const card of c.items) {
      seen.add(card.id);
      let entry = frames[card.id];
      if (!entry) {
        const b = box(card);
        entry = frames[card.id] = {v: "", box: b, frame: b.querySelector("iframe")};
        entry.frame.addEventListener("load", () => send(entry));
      }
      if (entry.box.parentNode !== list) list.appendChild(entry.box);   // once: moving a frame would reload it
      entry.box.querySelector("h2").textContent = card.title;
      entry.box.querySelector("h2").title = "You asked: " + card.request;
      entry.frame.title = card.title;
      entry.box.querySelector('[data-card="change"]').disabled = !!c.making;
      if (entry.v !== card.v) { entry.v = card.v; load(card.id, entry); }
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
    const msg = document.getElementById("msg");
    msg.textContent = "…";
    const r = await post("/console/action", {room, action, value, agent: ""});
    msg.textContent = r.result === "ok" ? (action === "card_remove" ? "Removed." : "Asked your agent. It takes a minute or two.") : (r.result || r.error);
    ctlSig = "";
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
      const f = cardBox.querySelector("[data-change]");
      f.hidden = !f.hidden;
      if (!f.hidden) f.querySelector("input").focus();
    } else if (b.dataset.card === "change-send") {
      const input = cardBox.querySelector("[data-change] input"), text = input.value.trim();
      if (!text) return void input.focus();
      if (await cardAct("card_change", id + "\n" + text)) { input.value = ""; cardBox.querySelector("[data-change]").hidden = true; }
    } else if (b.dataset.card === "remove") {
      if (b.dataset.armed !== "1") {         // two clicks: a card is gone for good
        b.dataset.armed = "1"; b.textContent = "Sure? Remove";
        setTimeout(() => { b.dataset.armed = ""; b.textContent = "Remove"; }, ARM_MS);
        return;
      }
      cardAct("card_remove", id);
    }
  });
  document.addEventListener("keydown", e => {   // Enter in a card's box clicks its button
    if (e.key !== "Enter" || !e.target.closest || !e.target.closest("#cards") || e.target.tagName !== "INPUT") return;
    const btn = e.target.closest(".field").querySelector("button");
    if (btn && !btn.disabled) btn.click();
  });
})();
