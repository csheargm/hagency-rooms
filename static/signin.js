// Sign-in for Hagency's own pages (server/signin_page.py; docs/adr/0014 and 0015): the invite page,
// Get your agent and Make an invite link. The browser talks straight to our homeserver (its address
// is in the page's meta tag): Google or GitHub through its SSO, or a username and password to its
// /login. Our server never sees a password. The invite page shows it an OpenID token; the newer
// pages show it a throwaway session once and sign it out right after (handOver). A loginToken coming back from SSO counts only in the tab that started
// that very sign-in: the button stores a random nonce in this tab and puts it in the return address,
// and only a return carrying it counts (GPT review), so a crafted link with someone else's login
// token is ignored.
"use strict";
const SSO_PARAM = "sso";

/** This tab's own SSO return: {idp, token} when `query` carries the nonce `mark` stored (recently), else null. */
function ownReturn(mark, query, now) {
  const token = query.get("loginToken"), nonce = query.get(SSO_PARAM);
  const fresh = mark && typeof mark.idp === "string" && typeof mark.nonce === "string" && mark.nonce.length >= 32 && now < mark.until;
  return token && fresh && nonce === mark.nonce ? {idp: mark.idp, token} : null;
}

const SignIn = typeof document === "undefined" ? null : (() => {
  const meta = n => document.querySelector(`meta[name="${n}"]`).content;
  const HS = meta("hagency-homeserver"), ELEMENT = meta("hagency-element");
  const SSO_MS = 10 * 60 * 1000;
  const $ = id => document.getElementById(id);
  const store = {get: k => { try { return sessionStorage.getItem(k); } catch (e) { return null; } },   // blocked: just this visit
    set: (k, v) => { try { sessionStorage.setItem(k, v); } catch (e) { /* blocked: kept in this visit only */ } },
    drop: k => { try { sessionStorage.removeItem(k); } catch (e) { /* blocked: nothing was kept */ } }};

  async function post(path, body, auth) {
    const r = await fetch(path, {method: "POST", body: JSON.stringify(body),
      headers: {"Content-Type": "application/json", ...(auth ? {Authorization: "Bearer " + auth} : {})}});
    let data = {}; try { data = await r.json(); } catch (e) { /* not JSON: judged by the status */ }
    return {status: r.status, data};
  }
  const hs = (path, body, auth) => post(HS + "/_matrix/client/v3" + path, body, auth);
  const ssoUrl = (idp, back) => `${HS}/_matrix/client/v3/login/sso/redirect/${encodeURIComponent(idp)}?redirectUrl=${encodeURIComponent(back)}`;

  /** This tab's SSO mark ({idp, until, nonce}), used up by reading it. */
  function takeMark(key) {
    let mark = null; try { mark = JSON.parse(store.get(key) || "null"); } catch (e) { /* damaged: none */ }
    store.drop(key);
    return mark;
  }
  function nonce() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
  }

  /** A "Continue with …" button in `box` for each provider our homeserver lists; how many there are. */
  async function ssoButtons(box, key) {
    let flows = [];
    try { flows = (await (await fetch(HS + "/_matrix/client/v3/login")).json()).flows || []; } catch (e) { return 0; }
    const sso = flows.find(f => f.type === "m.login.sso");
    const idps = ((sso && sso.identity_providers) || []).filter(p => /^[A-Za-z0-9._~-]{1,64}$/.test(p.id));
    for (const p of idps) {
      const b = document.createElement("button");
      b.textContent = "Continue with " + String(p.name || p.id).slice(0, 40);
      b.className = "primary";
      b.addEventListener("click", () => { const n = nonce();
        store.set(key, JSON.stringify({idp: p.id, until: Date.now() + SSO_MS, nonce: n}));
        location.assign(ssoUrl(p.id, `${location.origin}${location.pathname}?${SSO_PARAM}=${n}`)); });
      box.appendChild(b);
    }
    return idps.length;
  }

  /** The sign-in SSO brought back to this tab ({login, idp}), or null. The mark is used up and the
      query (loginToken, nonce) leaves the address bar either way; the fragment stays. */
  function returned(key) {
    const query = new URLSearchParams(location.search), mine = ownReturn(takeMark(key), query, Date.now());
    if (query.has("loginToken") || query.has(SSO_PARAM)) history.replaceState(null, "", location.pathname + location.hash);
    return mine ? {login: {type: "m.login.token", token: mine.token}, idp: mine.idp} : null;
  }

  /** What the password form says: m.login.password for this username. */
  const password = (user, pass) => ({type: "m.login.password", identifier: {type: "m.id.user", user: user.trim()}, password: pass});

  /** The username-and-password form, built into `box`; onLogin(m.login.password body) on submit. */
  function form(box, button, onLogin) {
    const el = (tag, props) => Object.assign(document.createElement(tag), props);
    const user = el("input", {id: "user", name: "username", autocomplete: "username", autocapitalize: "none",
      spellcheck: false, required: true});
    const pass = el("input", {id: "pass", name: "password", type: "password", autocomplete: "current-password", required: true});
    const f = el("form", {id: "password", autocomplete: "on"});
    f.append(el("h2", {textContent: "Sign in with your username and password"}), el("label", {htmlFor: "user", textContent: "Username"}),
      user, el("label", {htmlFor: "pass", textContent: "Password"}), pass, el("button", {type: "submit", className: "primary", textContent: button}));
    f.addEventListener("submit", e => { e.preventDefault(); onLogin(password(user.value, pass.value)); });
    box.appendChild(f);
  }

  /** {session, user} for a sign-in, or {error: "wrong" | "offline" | "refused"}. The session asks for a
      refresh token, which this page never keeps: its access token then expires by itself within
      minutes (Synapse's refreshable_access_token_lifetime), even if the tab closes before the sign-out
      (GPT review). */
  async function login(body, device) {
    let r;
    try { r = await hs("/login", {...body, initial_device_display_name: device, refresh_token: true}); }
    catch (e) { return {error: "offline"}; }
    if (r.status !== 200 || !r.data.access_token) return {error: body.type === "m.login.password" ? "wrong" : "refused"};
    return {session: r.data.access_token, user: r.data.user_id};
  }

  /** An OpenID token for this session (it proves who signed in, for an hour), or null. */
  async function openid(session, user) {
    try { return (await hs(`/user/${encodeURIComponent(user)}/openid/request_token`, {}, session)).data.access_token || null; }
    catch (e) { return null; }
  }
  const logout = session => hs("/logout", {}, session).catch(() => null);

  /** Sign in with a new, throwaway session, show it to @pm once at `path` (server/page_session.py:
      it asks our homeserver whose it is and answers with its own page session), then sign it out
      again: @pm's answer {status, data}, or {error}. */
  async function handOver(body, device, path) {
    const s = await login(body, device);
    if (s.error) return s;
    try { return await post(path, {access_token: s.session}); }
    catch (e) { return {error: "offline"}; }
    finally { await logout(s.session); }
  }

  return {HS, ELEMENT, $, store, post, hs, ssoUrl, ssoButtons, returned, form, login, openid, logout, handOver};
})();

if (typeof module === "object" && module.exports) module.exports = {ownReturn, SSO_PARAM};   // tests/test_home_steps_js.py
