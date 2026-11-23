// Make an invite link (server/invite_new_page.py, docs/adr/0015-one-click-home-steps.md): sign in
// (static/signin.js), pick one of the project rooms where you're an owner or maintainer, and get the
// same one-person link "@pm invite link" makes (ADR 0014). The link is shown on this page only.
"use strict";
const IDP_KEY = "hagency-invite-new-idp";
const LINK_RE = /^(https:\/\/[A-Za-z0-9.-]+|http:\/\/(localhost|127\.0\.0\.1))(:\d{1,5})?\/invite#[A-Za-z0-9_-]{20,128}$/;   // invite_page.link, nothing else
const SAYS = {offline: "We couldn't reach Hagency Rooms. Check your connection and try again.",
  wrong: "That username and password didn't work. Please try again.",
  refused: "We couldn't confirm your sign-in. Please try again.",
  not_signed_in: "Your sign-in has run out. Please sign in again.",
  busy: "Too many tries from here. Please wait a few minutes and try again.",
  failed: "Something went wrong. Please try again in a minute.",
  none: "You're not an owner or maintainer of a project room yet. Create a room first (step ②), or ask " +
    "the room's owner to make the link."};

/** What to say under a new link (pm_invite.LINK_DM, in fewer words). */
function aboutText(days, id) {
  return `It lets one person in, once, within ${days} day${days === 1 ? "" : "s"}. Send it only to the person ` +
    `you're inviting. To cancel it, tell the Product Manager "cancel invite link ${id}".`;
}

/** The link to show, or "" when it isn't one of ours. */
const shownLink = link => (LINK_RE.test(String(link || "")) ? link : "");

// The page itself (browser only; SignIn is static/signin.js).
let openid = null;
const $ = id => document.getElementById(id);
const say = (text, ok) => { const s = $("status"); s.textContent = text || ""; s.hidden = !text; s.className = ok ? "status ok" : "status"; };
const signInAgain = text => { openid = null; say(text); $("make").hidden = $("made").hidden = true; $("signin").hidden = false; };
const why = r => r.data.error || SAYS[r.data.state] || (r.status ? SAYS.failed : SAYS.offline);

async function ask(path, body) {
  try { return await SignIn.post(path, {openid, ...body}); } catch (e) { return {status: 0, data: {}}; }
}

async function signedIn(body) {
  $("signin").hidden = true; say("Signing you in…", true);
  const r = await SignIn.openidOnly(body, "Hagency: invite people");
  if (r.error) { say(SAYS[r.error]); $("signin").hidden = false; return; }
  openid = r.openid;
  const rooms = await ask("/invite/new/rooms", {});
  if (rooms.status === 401) return signInAgain(SAYS.not_signed_in);
  if (rooms.data.state !== "ok" || !Array.isArray(rooms.data.rooms)) return signInAgain(why(rooms));   // a way to try again
  if (!rooms.data.rooms.length) return say(SAYS.none);
  say("");
  $("room").replaceChildren(...rooms.data.rooms.map(room => new Option(String(room.name), String(room.id))));
  $("make").hidden = false;
}

async function go() {
  $("go").disabled = true;
  const r = await ask("/invite/new/make", {room: $("room").value, days: Number($("days").value)});
  $("go").disabled = false;
  if (r.status === 401) return signInAgain(SAYS.not_signed_in);
  const link = r.data.state === "ok" ? shownLink(r.data.link) : "";
  if (!link) return say(why(r));
  say("");
  $("link").textContent = link;
  $("about").textContent = aboutText(Number(r.data.days), String(r.data.id || ""));
  $("copy").textContent = "Copy link";
  $("made").hidden = false;
}

function copy() {
  const done = () => { $("copy").textContent = "Copied ✓"; };
  const manual = () => {                 // select it, so Ctrl/Cmd-C works where the clipboard says no
    const range = document.createRange(); range.selectNodeContents($("link"));
    const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
    $("copy").textContent = "Press Ctrl+C (⌘C on a Mac) to copy";
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText($("link").textContent).then(done, manual);
  else manual();
}

function start() {
  $("go").addEventListener("click", go);
  $("copy").addEventListener("click", copy);
  SignIn.form($("password-box"), "Sign in", signedIn);
  SignIn.ssoButtons($("sso"), IDP_KEY);    // there for a retry too (GPT review)
  const back = SignIn.returned(IDP_KEY);   // Google or GitHub brought this tab back: finish that sign-in
  if (back) return signedIn(back.login);
  $("signin").hidden = false;
}

if (typeof module === "object" && module.exports) {   // tests/test_home_steps_js.py runs these under node
  module.exports = {aboutText, shownLink};
} else {
  document.addEventListener("DOMContentLoaded", start);
}
