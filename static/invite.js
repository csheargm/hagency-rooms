// The invite page (server/invite_page.py, docs/adr/0014-one-step-invite-link.md). The invite token is
// in the URL fragment (never sent to a server); it is kept in this tab's sessionStorage while the
// visitor signs in with Google or GitHub, and sent only in the bodies of invite/check and
// invite/redeem. Sign-in goes straight to the homeserver; this page's session is signed out before
// Element opens. The only places this page ever sends anyone: the homeserver's own SSO address and
// Element at the room @pm names (both fixed origins from the page's meta tags).
"use strict";
const meta = n => document.querySelector(`meta[name="${n}"]`).content;
const HS = meta("hagency-homeserver"), ELEMENT = meta("hagency-element");
const KEY = "hagency-invite", IDP_KEY = "hagency-invite-idp";
const $ = id => document.getElementById(id);
const store = {get: k => { try { return sessionStorage.getItem(k); } catch (e) { return null; } },   // blocked: just this visit
  set: (k, v) => { try { sessionStorage.setItem(k, v); } catch (e) { /* blocked: the link stays in this visit only */ } },
  drop: k => { try { sessionStorage.removeItem(k); } catch (e) { /* blocked: nothing was kept */ } }};
const AGAIN = " Ask the person who sent it for a new one.";
const SAYS = {used: "This invite link has already been used." + AGAIN, expired: "This invite link has expired." + AGAIN,
  cancelled: "This invite link was cancelled." + AGAIN,
  unknown: "This invite link doesn't work. Check that you copied all of it, or ask the person who sent it for a new one.",
  none: "This page needs the invite link you were sent. Open that link again.",
  busy: "Too many tries from here. Please wait a few minutes and try again.",
  failed: "We couldn't add you to the room just now. Please try again in a minute: your link still works.",
  not_signed_in: "We couldn't confirm your sign-in. Please try again.",
  offline: "We couldn't reach Hagency Rooms. Check your connection and try again."};
let token = null;

function say(text, ok) { const s = $("status"); s.textContent = text; s.hidden = !text; s.className = ok ? "status ok" : "status"; }
function stop(text) { say(text); $("signin").hidden = true; $("lead").textContent = ""; }
async function post(path, body, auth) {
  const r = await fetch(path, {method: "POST", body: JSON.stringify(body),
    headers: {"Content-Type": "application/json", ...(auth ? {Authorization: "Bearer " + auth} : {})}});
  let data = {}; try { data = await r.json(); } catch (e) { /* not JSON: judged by the status below */ }
  return {status: r.status, data};
}
const hs = (path, body, auth) => post(HS + "/_matrix/client/v3" + path, body, auth);
const elementHere = () => { try { return new URL(ELEMENT).origin === location.origin; } catch (e) { return false; } };

async function ssoChoices() {
  let flows = [];
  try { flows = (await (await fetch(HS + "/_matrix/client/v3/login")).json()).flows || []; } catch (e) { return; }
  const sso = flows.find(f => f.type === "m.login.sso");
  const idps = ((sso && sso.identity_providers) || []).filter(p => /^[A-Za-z0-9._~-]{1,64}$/.test(p.id));
  for (const p of idps) {
    const b = document.createElement("button");
    b.textContent = "Continue with " + String(p.name || p.id).slice(0, 40);
    b.className = "primary";
    b.addEventListener("click", () => { store.set(IDP_KEY, p.id); location.assign(ssoUrl(p.id, location.origin + location.pathname)); });
    $("sso").appendChild(b);
  }
  $("new-sso").hidden = !idps.length;
}
const ssoUrl = (idp, back) => `${HS}/_matrix/client/v3/login/sso/redirect/${encodeURIComponent(idp)}?redirectUrl=${encodeURIComponent(back)}`;

async function signIn(login) {
  $("signin").hidden = true; say("Signing you in…", true);
  let r;
  try { r = await hs("/login", {...login, initial_device_display_name: "Hagency invite link"}); } catch (e) { return retry(SAYS.offline); }
  if (r.status !== 200 || !r.data.access_token) return retry(login.type === "m.login.password"
    ? "That username and password didn't work. Please try again." : SAYS.not_signed_in);
  const {access_token: session, user_id: user} = r.data;
  let room = null;
  try { room = await join(session, user); } catch (e) { retry(SAYS.offline); } finally { await hs("/logout", {}, session).catch(() => null); }   // this page's session, gone
  if (room) openElement(room);                     // only after the sign-out: leaving the page would cut it short
}
function retry(text) { say(text); $("signin").hidden = false; store.drop(IDP_KEY); }

async function join(session, user) {              // the room's ID once they're in, else nothing (the page says why)
  const id = await hs(`/user/${encodeURIComponent(user)}/openid/request_token`, {}, session);
  if (!id.data.access_token) return retry(SAYS.not_signed_in);
  say("Adding you to the room…", true);
  const r = await post("invite/redeem", {token, openid: id.data.access_token});
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

function openElement(room) {
  const target = `${ELEMENT}/#/room/${encodeURIComponent(room)}`;
  const idp = store.get(IDP_KEY); store.drop(IDP_KEY);
  let signedIn = true;
  if (elementHere()) { try { signedIn = !!localStorage.getItem("mx_user_id"); } catch (e) { /* blocked: assume signed in */ } }
  if (idp && elementHere() && !signedIn) {   // the app finishes this sign-in itself (its own SSO hand-back)
    try { localStorage.setItem("mx_sso_hs_url", HS); } catch (e) { return location.assign(target); }
    return location.assign(ssoUrl(idp, target));
  }
  location.assign(target);
}

async function start() {
  const fromLink = location.hash.slice(1);
  const back = new URLSearchParams(location.search).get("loginToken");
  if (fromLink || back) history.replaceState(null, "", location.pathname);   // neither stays in the address bar
  if (fromLink) store.set(KEY, fromLink);
  token = fromLink || store.get(KEY);
  if (!token) return stop(SAYS.none);
  let r;
  try { r = await post("invite/check", {token}); } catch (e) { return stop(SAYS.offline); }
  if (r.data.state !== "ok") return stop(SAYS[r.data.state] || SAYS.unknown);
  $("title").textContent = `Join ${r.data.room}`;
  $("lead").textContent = `${r.data.by} invited you to ${r.data.room} on Hagency Rooms. Sign in or create an account, and you're in.`;
  $("register").href = `${ELEMENT}/#/register`;
  if (back) return signIn({type: "m.login.token", token: back});
  $("signin").hidden = false;
  await ssoChoices();
}

document.addEventListener("DOMContentLoaded", () => {
  $("password").addEventListener("submit", e => { e.preventDefault();
    signIn({type: "m.login.password", identifier: {type: "m.id.user", user: $("user").value.trim()}, password: $("pass").value}); });
  start();
});
window.addEventListener("hashchange", () => location.reload());   // another link pasted into this tab: start over
