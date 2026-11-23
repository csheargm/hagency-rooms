// Sign-in for Hagency's own pages (server/signin_page.py; docs/adr/0014 and 0015): the invite page,
// Get your agent and Make an invite link. The browser talks straight to our homeserver (its address
// is in the page's meta tag): Google or GitHub through its SSO, or a username and password to its
// /login. Our server never sees a password or a session, only an OpenID token, which proves who
// signed in and nothing more. A loginToken coming back from SSO counts only in the tab that started
// that sign-in (it is marked when the button is clicked), so a crafted link carrying someone else's
// login token is ignored.
"use strict";
const SignIn = (() => {
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

  /** The provider this tab's own SSO sign-in used, if it started one lately; used up by reading it. */
  function startedSso(key) {
    let mark = null; try { mark = JSON.parse(store.get(key) || "null"); } catch (e) { /* damaged: none */ }
    store.drop(key);
    return mark && typeof mark.idp === "string" && Date.now() < mark.until ? mark.idp : null;
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
      b.addEventListener("click", () => { store.set(key, JSON.stringify({idp: p.id, until: Date.now() + SSO_MS}));
        location.assign(ssoUrl(p.id, location.origin + location.pathname)); });
      box.appendChild(b);
    }
    return idps.length;
  }

  /** A loginToken SSO brought back to this tab, or null; the address bar is cleaned either way. */
  function returned(key) {
    const idp = startedSso(key);
    const back = new URLSearchParams(location.search).get("loginToken");
    if (back) history.replaceState(null, "", location.pathname);
    return back && idp ? {login: {type: "m.login.token", token: back}, idp} : null;
  }

  /** What the password form says: m.login.password for this username. */
  const password = (user, pass) => ({type: "m.login.password", identifier: {type: "m.id.user", user: user.trim()}, password: pass});

  /** {session, user} for a sign-in, or {error: "wrong" | "offline" | "refused"}. */
  async function login(body, device) {
    let r;
    try { r = await hs("/login", {...body, initial_device_display_name: device}); } catch (e) { return {error: "offline"}; }
    if (r.status !== 200 || !r.data.access_token) return {error: body.type === "m.login.password" ? "wrong" : "refused"};
    return {session: r.data.access_token, user: r.data.user_id};
  }

  /** An OpenID token for this session (it proves who signed in, for an hour), or null. */
  async function openid(session, user) {
    try { return (await hs(`/user/${encodeURIComponent(user)}/openid/request_token`, {}, session)).data.access_token || null; }
    catch (e) { return null; }
  }
  const logout = session => hs("/logout", {}, session).catch(() => null);

  /** Sign in, take an OpenID token and sign this page's session out again: {openid} or {error}. */
  async function openidOnly(body, device) {
    const s = await login(body, device);
    if (s.error) return s;
    try { const token = await openid(s.session, s.user); return token ? {openid: token} : {error: "refused"}; }
    finally { await logout(s.session); }
  }

  return {HS, ELEMENT, $, store, post, hs, ssoUrl, ssoButtons, returned, password, login, openid, logout, openidOnly};
})();
