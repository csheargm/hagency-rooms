// "GitHub" in the Hagency panel and console (panel.py serves this as /panel-github.js; ADR 0007).
// Connect GitHub proves which GitHub account is yours (GitHub's device flow): @pm shows a code here
// and in your private chat, you enter it on github.com, and the card then says "GitHub: @login ✓".
// s.github comes from /console/state for the signed-in person only; null: this server doesn't use it.
let copiedUntil = 0;                      // "Copied ✓" stays this long, over the panel's refreshes
function githubCard(s) {
  const g = s.github;
  if (!g) return "";
  let body = "";
  if (g.login) body += `<div class="set"><div>GitHub: <b>@${esc(g.login)}</b> ✓</div>
    <button data-a="github" data-v="disconnect">Disconnect GitHub</button></div>`;
  if (g.code) body += `<div class="set"><div>Open <a href="${esc(g.url)}" target="_blank" rel="noopener noreferrer">${esc(g.url)}</a>
    and enter <input id="ghcode" class="code" readonly size="10" value="${esc(g.code)}">
    <button data-copy="${esc(g.code)}">${Date.now() < copiedUntil ? "Copied ✓" : "Copy"}</button></div>${
    g.login ? "" : '<button data-a="github" data-v="disconnect">Cancel</button>'}</div>
    <div class="hint">Waiting for you to approve on GitHub… Enter the code only if you asked for it here. It's also in
    ${s.personal ? "this chat" : typeof pmChatLink === "function" ? pmChatLink("your chat with Product Manager") : "your chat with Product Manager"}.</div>`;
  else if (!g.login) body += `<div class="set"><div>Show @pm which GitHub account is yours.</div>
    <button class="primary" data-a="github" data-v="connect">Connect GitHub</button></div>`;
  return `<section class="card"><h2>GitHub</h2>${body}<div class="hint">Choosing a room's repo needs write access to it
    on GitHub. @pm reads only your username.</div></section>`;
}
// Copy: the clipboard API, which Element's widget frame may refuse; then the code is selected in its
// field instead (the panel doesn't redraw a focused field), so Ctrl+C / ⌘C copies it.
async function copyCode(b, field, clipboard) {
  try {
    await clipboard.writeText(b.dataset.copy);
    copiedUntil = Date.now() + 2000;
    b.textContent = "Copied ✓";
  } catch (e) {                           // refused or missing: selecting it is the fallback, said on the button
    if (field) { field.focus(); field.select(); }
    b.textContent = (/Mac/.test(navigator.platform) ? "Selected: press ⌘C" : "Selected: press Ctrl+C");
  }
}
// The line under the buttons after Connect GitHub ("Your code is on the GitHub card…") is stale once
// the code has gone (connected, expired or cancelled): panel.html asks this on every view.
// pending: Connect was clicked; shown: its code appeared. Returns the next mark, and "" to clear the line.
function githubMsgAfter(mark, s) {
  const code = !!(s.github && s.github.code);
  if (mark === "pending") return code ? "shown" : s.github && s.github.login ? "" : "pending";   // connected before a view
  if (mark === "shown") return code ? "shown" : "";
  return mark;
}
if (typeof document !== "undefined") document.addEventListener("click", e => {
  const b = e.target.closest && e.target.closest("button[data-copy]");
  if (b) copyCode(b, document.getElementById("ghcode"), navigator.clipboard || {writeText: () => Promise.reject(new Error("none"))});
});
