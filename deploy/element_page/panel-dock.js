/* A "Hagency panel" button in Element when our panel is minimised (docs/adr/0026). Linked into Element's
   index.html by element-nginx.conf (sub_filter), served by Caddy at /hagency/ (element_home.STATIC).

   Element (v1.12.35) moves a minimised widget to the room's right-side "Extensions" list and shows no trace of
   it above the chat; the way back is Room info → Extensions → the pin next to Hagency, which owners didn't find.
   This button does what that pin does, through Element's own widget layout store (mxSdkContext: Element's
   global), so it is the person's own layout, in this room only, as Element saves it; nobody else's changes.
   Only our own panel: the widget "hagency" whose page is this server's /panel. Anything unexpected (another
   Element version without these internals) and the button simply isn't shown: Element works as before. */
(function () {
  const WIDGET_ID = "hagency", PANEL_PATH = "/panel", TICK_MS = 1000, GAP_PX = 6;
  const LABEL = "▾ Hagency panel";
  const TIP = "Put the Hagency panel back above the chat (it was minimised).";
  const FULL = "Element's top area is full: minimise another widget first, then click here.";
  let button = null;

  const ours = w => {
    try { const u = new URL(w.url, location.href); return w.id === WIDGET_ID && u.origin === location.origin && u.pathname === PANEL_PATH; }
    catch (e) { return false; }           // a widget URL that isn't a URL: not our panel
  };
  // The room on screen and our minimised panel in it, or null.
  function minimised() {
    const ctx = window.mxSdkContext;
    if (!ctx || !ctx.widgetLayoutStore || !ctx.roomViewStore || !ctx.client) return null;
    const id = ctx.roomViewStore.getRoomId(), room = id && ctx.client.getRoom(id);
    if (!room) return null;
    const app = ctx.widgetLayoutStore.getContainerWidgets(room, "right").find(ours);
    return app ? {store: ctx.widgetLayoutStore, room, app} : null;
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
    button.addEventListener("click", () => {
      const now = minimised();
      if (now && now.store.canAddToContainer(now.room, "top")) now.store.moveToContainer(now.room, now.app, "top");
      tick();
    });
    document.body.appendChild(button);
    return button;
  }
  function tick() {
    let now = null;
    try { now = minimised(); }
    catch (e) { now = null; }             // Element's internals changed: no button, Element's own pin still works
    const header = document.querySelector(".mx_RoomView .mx_RoomHeader");
    if (!now || !header) { if (button) button.hidden = true; return; }
    const b = draw(), box = header.getBoundingClientRect(), full = !now.store.canAddToContainer(now.room, "top");
    const banner = document.querySelector(".mx_RoomView .mx_PinnedMessageBanner");   // live: it sat on the pinned
    const below = Math.max(box.bottom, banner ? banner.getBoundingClientRect().bottom : 0);   // message's text
    b.hidden = false;
    b.title = full ? FULL : TIP;
    b.setAttribute("aria-label", b.title);
    b.style.top = Math.round(below + GAP_PX) + "px";
    b.style.right = Math.round(window.innerWidth - box.right + 2 * GAP_PX) + "px";
  }
  setInterval(tick, TICK_MS);
})();
