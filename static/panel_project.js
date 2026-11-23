// "Who can join" in the Hagency panel and console (panel.py serves this as /panel-project.js), and
// where this room's repo is run from when it only discusses it (one project room per repo:
// pm_projects.py). Values come from /console/state and are escaped here; changes go through @pm's
// own checks (console.act → set_visibility: owners and maintainers only, never "open" on a private repo).
// "Project repo": choose it, check it, or remove it (pm_unbind.py: owners and maintainers; the
// page asks twice, as Remove an agent does, since Element's widget frame may block confirm()).
// Each help text starts with its button's own label (a screen test saw "Private" selected over "Unlisted: ...").
const VISIBILITY = {private: "Private: not listed anywhere. People join when invited, or by asking @pm with the exact repo name.",
  listed: "Listed: shown in Find a project and the room directory. People ask to join, and an owner lets them in.",
  open: "Open: listed, and anyone can join right away."};
function projectCard(s) {
  let h = "";
  if (s.runs_in) h += `<section class="card attn"><h2>Run from another room</h2><div>This repo is run from
    ${s.runs_in.link ? `<a href="${esc(s.runs_in.link)}" target="_blank" rel="noopener">${esc(s.runs_in.name)}</a>`
      : esc(s.runs_in.name)}: this room discusses it, and its mode stays off.</div></section>`;
  const vis = s.visibility || "private";
  h += `<section class="card"><h2>Who can join</h2>${buttons("visibility", ["private", "listed", "open"], vis, s.moderator)}
    <div class="hint">${esc(VISIBILITY[vis] || "")}${s.moderator ? "" : " Only the owner or a maintainer can change it."}</div>
    ${s.moderator ? `<div class="hint"><button data-a="invite_link">Make an invite link</button> I'll send it to our private
      chat. It lets one person in, once, within 7 days, even if they don't have an account yet.</div>` : ""}</section>`;
  return h;
}
let unrepoArmed = {repo: "", until: 0};   // the repo a first click armed, until when (GPT review: never
function repoCard(s) {                    // a repo bound since; a different one shows unarmed)
  const armed = Date.now() < unrepoArmed.until && unrepoArmed.repo === s.repo;
  const choose = s.moderator ? `<div class="field"><input id=repo placeholder="owner/name" value="${esc(s.repo || "")}">
    <button data-a="repo">Use for this room</button></div>` : `<div>${esc(s.repo || "None yet")}</div>`;
  const more = !s.repo ? '<div class="hint">One GitHub repo per room.</div>'
    : `<div class="hint"><button data-a="check">Check readiness</button>${s.moderator ? ` <button data-a="unrepo"
      data-v="${armed ? "sure:" + esc(s.repo) : ""}" data-g="${esc(s.repo)}">${armed ? "Click again to remove the repo" : "Remove repo"}</button>` : ""}</div>`;
  return `<section class="card"><h2>${s.repo ? "Project repo" : "Pick a project repo"}</h2>${choose}${more}</section>`;
}
function repoConfirmed(b) {              // panel.html's click handler: false = this click only armed it
  const same = unrepoArmed.repo === b.dataset.g && b.dataset.v === "sure:" + b.dataset.g;
  if (same && Date.now() < unrepoArmed.until) { unrepoArmed = {repo: "", until: 0}; return true; }
  unrepoArmed = {repo: b.dataset.g, until: Date.now() + 8000};
  b.dataset.v = "sure:" + b.dataset.g;   // the repo this card showed: a newer one is never removed by it
  b.textContent = "Click again to remove the repo";
  return false;
}
// "Mode", after "Project repo" (owner request): with no repo yet only Off can be clicked, and the hint
// says to pick the repo first (the server refuses assist / autonomous without one too: pm_settings.NO_REPO).
const MODES = {off: "Paused: no new work is handed out and nothing is merged.",
  assist: "Agents build and review; a person merges each change.",
  autonomous: "Agents build, review and merge on their own."};
function modeCard(s) {
  const live = v => !!s.moderator && (v === "off" || !!s.repo);
  const hint = s.repo ? esc(MODES[s.mode] || "") : "Pick the project repo first.";
  return `<section class="card"><h2>Mode</h2>${buttons("mode", ["off", "assist", "autonomous"], s.mode, live)}
    <div class="hint">${hint}${s.moderator ? "" : " Only the owner or a maintainer can change it."}</div></section>`;
}
