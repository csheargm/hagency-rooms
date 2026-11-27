/* A "Hagency panel" button in Element when our panel is minimised (docs/adr/0026). Linked into Element's
   index.html by element-nginx.conf (sub_filter), served by Caddy at /hagency/ (element_home.STATIC; element_home
   fills in @pm's ID and the panel's URL template).

   Element (v1.12.35) moves a minimised widget to the room's right-side "Extensions" list and shows no trace of
   it above the chat; the way back is Room info → Extensions → the pin next to Hagency, which owners didn't find.
   This button does what that pin does, through Element's own widget layout store (mxSdkContext: Element's
   global), so it is the person's own layout, in this room only, as Element saves it; nobody else's changes.
   Only our own panel: the widget "hagency" that @pm added, at exactly our panel's URL (GPT review: a member who
   may write widgets could put a lookalike there). Anything unexpected (another Element version without these
   internals) and the button simply isn't shown: Element works as before. */
(function () {
  const WIDGET_ID = "hagency", PM = "__PM_ID__", PANEL_URL = location.origin + "__PANEL_TEMPLATE__";
  const TICK_MS = 1000, GAP_PX = 6;
  const LABEL = "▾ Hagency panel";
  const TIP = "Put the Hagency panel back above the chat (it was minimised).";
  const FULL = "Element's top area is full: minimise another widget first, then click here.";
  let button = null, shownFor = "";        // the room the button was drawn for (GPT review: never another one)

  const ours = w => w.id === WIDGET_ID && w.url === PANEL_URL && w.creatorUserId === PM;
  // The room on screen and our minimised panel in it, or null.
  function minimised() {
    const ctx = window.mxSdkContext;
    if (!ctx || !ctx.widgetLayoutStore || !ctx.roomViewStore || !ctx.client) return null;
    const id = ctx.roomViewStore.getRoomId(), room = id && ctx.client.getRoom(id);
    if (!room) return null;
    const app = ctx.widgetLayoutStore.getContainerWidgets(room, "right").find(ours);
    return app ? {store: ctx.widgetLayoutStore, room, app} : null;
  }
  function redock() {
    try {
      const now = minimised();
      if (now && now.room.roomId === shownFor && now.store.canAddToContainer(now.room, "top"))
        now.store.moveToContainer(now.room, now.app, "top");   // as Element's pin: a maximised widget goes to the side
    } catch (e) { /* Element's internals changed: nothing moved; tick() hides the button */ }
    tick();
  }
  function draw() {
    if (button) return button;
    button = document.createElement("button");
    button.type = "button";
    button.id = "hagency-dock";
    button.textContent = LABEL;
    button.style.cssText = "position:fixed;z-index:20;padding:4px 12px;border-radius:999px;cursor:pointer;"
      + "font:600 13px/1.4 Inter,system-ui,sans-serif;border:1px solid var(--cpd-color-border-interactive-secondary,#c0c4cc);"
      + "background:var(--cpd-color-bg-canvas-default,#fff);color:var(--cpd-color-text-primary,#1b1d22);"
      + "box-shadow:0 1px 4px rgba(0,0,0,.15)";
    button.addEventListener("click", redock);
    document.body.appendChild(button);
    return button;
  }
  function place(b) {                      // under the room header, and under its pinned message if any
    const header = document.querySelector(".mx_RoomView .mx_RoomHeader");
    if (!header) return false;
    const box = header.getBoundingClientRect(), banner = document.querySelector(".mx_RoomView .mx_PinnedMessageBanner");
    const below = Math.max(box.bottom, banner ? banner.getBoundingClientRect().bottom : 0);   // live: it sat on its text
    b.style.top = Math.round(below + GAP_PX) + "px";
    b.style.right = Math.round(window.innerWidth - box.right + 2 * GAP_PX) + "px";
    return true;
  }
  function tick() {
    let now = null, full = false;
    try { now = minimised(); full = !!now && !now.store.canAddToContainer(now.room, "top"); }
    catch (e) { now = null; }              // Element's internals changed: no button, Element's own pin still works
    if (!now) { shownFor = ""; if (button) button.hidden = true; return; }
    const b = draw();
    if (!place(b)) { b.hidden = true; return; }
    shownFor = now.room.roomId;
    b.hidden = false;
    b.title = full ? FULL : TIP;           // its name stays its words: "Hagency panel"
    b.setAttribute("aria-disabled", String(full));
  }
  setInterval(tick, TICK_MS);
})();
