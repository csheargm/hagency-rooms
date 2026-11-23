// The invite page (server/invite_page.py, docs/adr/0014-one-step-invite-link.md). The invite token is
// in the URL fragment (never sent to a server); it is kept in this tab's sessionStorage while the
// visitor signs in with Google or GitHub, and sent only in the bodies of invite/check and
// invite/redeem. Sign-in goes straight to the homeserver; this page's session is signed out before
// Element opens. The only places this page ever sends anyone: the homeserver's own SSO address and
// Element at the room @pm names (both fixed origins from the page's meta tags).
"use strict";
const {HS, ELEMENT, $, store, post, hs, ssoUrl} = SignIn;   // static/signin.js
const KEY = "hagency-invite", IDP_KEY = "hagency-invite-idp";
const AGAIN = " Ask the person who sent it for a new one.";
const SAYS = {used: "This invite link has already been used." + AGAIN, expired: "This invite link has expired." + AGAIN,
  cancelled: "This invite link was cancelled." + AGAIN,
  unknown: "This invite link doesn't work. Check that you copied all of it, or ask the person who sent it for a new one.",
  none: "This page needs the invite link you were sent. Open that link again.",
  busy: "Too many tries from here. Please wait a few minutes and try again.",
  closed: "This invite link no longer works." + AGAIN,
  unsure: "Something went wrong while adding you. If the room isn't in your list in a minute, ask the person who sent the link for a new one.",
  failed: "We couldn't add you to the room just now. Please try again in a minute: your link still works.",
  not_signed_in: "We couldn't confirm your sign-in. Please try again.",
  offline: "We couldn't reach Hagency Rooms. Check your connection and try again."};
let token = null;

function say(text, ok) { const s = $("status"); s.textContent = text; s.hidden = !text; s.className = ok ? "status ok" : "status"; }
function stop(text) { say(text); $("signin").hidden = true; $("lead").textContent = ""; }
const elementHere = () => { try { return new URL(ELEMENT).origin === location.origin; } catch (e) { return false; } };

async function signIn(login, idp) {
  $("signin").hidden = true; say("Signing you in…", true);
  const r = await SignIn.login(login, "Hagency invite link");
  if (r.error) return retry(r.error === "offline" ? SAYS.offline : r.error === "wrong"
    ? "That username and password didn't work. Please try again." : SAYS.not_signed_in);
  let room = null;
  try { room = await join(r.session, r.user); } catch (e) { retry(SAYS.offline); } finally { await SignIn.logout(r.session); }   // this page's session, gone
  if (room) openElement(room, idp);                     // only after the sign-out: leaving the page would cut it short
}
function retry(text) { say(text); $("signin").hidden = false; }

async function join(session, user) {              // the room's ID once they're in, else nothing (the page says why)
  const openid = await SignIn.openid(session, user);
  if (!openid) return retry(SAYS.not_signed_in);
  say("Adding you to the room…", true);
  const r = await post("invite/redeem", {token, openid});
  const state = r.data.state;
  if (state === "failed" || state === "not_signed_in") return retry(SAYS[state]);   // the link still works
  if (state !== "ok" && state !== "member") return stop(r.data.error || SAYS[state] || SAYS.unknown);
  const room = String(r.data.room || "");
  if (!/^![^/?#\s]+$/.test(room)) return stop(SAYS.failed);
  await hs(`/join/${encodeURIComponent(room)}`, {}, session).catch(() => null);   // failing that, the invite waits in the app
  store.drop(KEY);
  say("You're in! Opening the room…", true);
  return room;
}

function openElement(room, idp) {
  const target = `${ELEMENT}/#/room/${encodeURIComponent(room)}`;
  let signedIn = true;
  if (elementHere()) { try { signedIn = !!localStorage.getItem("mx_user_id"); } catch (e) { /* blocked: assume signed in */ } }
  if (idp && elementHere() && !signedIn) {   // the app finishes this sign-in itself (its own SSO hand-back)
    try { localStorage.setItem("mx_sso_hs_url", HS); } catch (e) { return location.assign(target); }
    return location.assign(ssoUrl(idp, target));
  }
  location.assign(target);
}

async function start() {
  const fromLink = location.hash.slice(1);   // read before anything cleans the address (GPT review)
  const back = SignIn.returned(IDP_KEY);   // the mark is used up first, whatever happens next (a new link clears it too)
  if (fromLink) history.replaceState(null, "", location.pathname);   // neither the token nor the query stays
  if (fromLink) store.set(KEY, fromLink);
  token = fromLink || store.get(KEY);
  if (!token) return stop(SAYS.none);
  let r;
  try { r = await post("invite/check", {token}); } catch (e) { return stop(SAYS.offline); }
  if (r.data.state !== "ok") return stop(SAYS[r.data.state] || SAYS.unknown);
  $("title").textContent = `Join ${r.data.room}`;
  $("lead").textContent = `${r.data.by} invited you to ${r.data.room} on Hagency Rooms. Sign in or create an account, and you're in.`;
  $("register").href = `${ELEMENT}/#/register`;
  // Only a sign-in this tab started (GPT review: a crafted link carrying someone else's login
  // token is ignored; a new tab has no IDP_KEY). The token is never used twice.
  const buttons = SignIn.ssoButtons($("sso"), IDP_KEY).then(n => { $("new-sso").hidden = !n; });   // for a retry too
  if (back && !fromLink) return signIn(back.login, back.idp);
  $("signin").hidden = false;
  await buttons;
}

document.addEventListener("DOMContentLoaded", () => {
  SignIn.form($("password-box"), "Sign in and join", login => signIn(login));
  start();
});
window.addEventListener("hashchange", () => location.reload());   // another link pasted into this tab: start over
