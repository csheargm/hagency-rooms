#!/usr/bin/env bash
# Deploy Hagency Rooms to the public VM, from GitHub only. Run on the operator's computer:
#   deploy/deploy.sh [COMMIT]        (default: the tip of master)
# COMMIT must be on csheargm/hagency-rooms's master (checked here, and again on the VM against a
# fresh fetch from GitHub). The VM (ssh host $HAGENCY_VM, default hagency-vm) checks it out in
# ~/hagency/app, clears ~/hagency/pm to that commit plus @pm's state, runs bootstrap.sh, then this
# checks the public URLs. Only the commit id is sent; secrets are made on the VM and never come back.
set -euo pipefail
cd "$(dirname "$0")/.."
VM="${HAGENCY_VM:-hagency-vm}"
REPO_URL="https://github.com/csheargm/hagency-rooms"
ROOMS="${ROOMS_DOMAIN:-rooms.hagencylabs.xyz}" MATRIX="${MATRIX_DOMAIN:-matrix.hagencylabs.xyz}"
ADMIN="${ADMIN_DOMAIN:-admin.hagencylabs.xyz}" SERVER="${SERVER_NAME:-${MATRIX_DOMAIN:-matrix.hagencylabs.xyz}}"

git fetch -q "$REPO_URL" master
master=$(git rev-parse FETCH_HEAD)
sha=$(git rev-parse --verify -q --end-of-options "${1:-$master}^{commit}") || { echo "✗ unknown commit: ${1:-}"; exit 2; }
git merge-base --is-ancestor "$sha" "$master" || { echo "✗ $sha is not on GitHub's master: refusing"; exit 2; }
echo "Deploying $sha to $VM"

ssh -- "$VM" bash -s -- "$sha" "$REPO_URL" <<'REMOTE'
set -euo pipefail
sha=$1 repo=$2 app=~/hagency/app pm=~/hagency/pm
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "✗ not a commit id"; exit 2; }
mkdir -p ~/hagency
exec 9>~/hagency/.deploy.lock
flock -n 9 || { echo "✗ another deploy is running"; exit 2; }
[ -d "$app/.git" ] || git clone -q "$repo" "$app"
git -C "$app" fetch -q "$repo" master                  # GitHub itself, whatever the clone's remotes say
git -C "$app" merge-base --is-ancestor "$sha" FETCH_HEAD || { echo "✗ $sha is not on master: refusing"; exit 2; }
edited=$(git -C "$app" status --porcelain --untracked-files=no)
[ -z "$edited" ] || { echo "✗ code was edited on the server: refusing"; exit 2; }
git -C "$app" checkout -q --detach "$sha"
grep -q 'PM_UID' "$app/deploy/docker-compose.yml" 2>/dev/null \
  || { echo "✗ $sha predates this deploy layout (release.py, PM_UID): deploy a newer commit"; exit 2; }
python3 "$app/deploy/envfile.py" check                 # this commit's settings, before anything stops
if [ -f "$app/deploy/.env" ]; then                     # @pm stops before its files change
  (cd "$app/deploy" && sudo docker compose --env-file .env stop pm >/dev/null)
fi
mkdir -p "$pm"
chmod 700 "$pm"
git -C "$app" ls-tree -r -z --name-only "$sha" > ~/hagency/.files
python3 "$app/deploy/release.py" "$pm" < ~/hagency/.files
git -C "$app" archive "$sha" | tar -x -C "$pm"         # the commit's files; @pm's state stays
git -C "$app" log -1 --format='%H%n%ct' "$sha" > "$pm/REVISION"   # the panel's version (common/version.py)
"$app/deploy/bootstrap.sh" </dev/null                    # its docker exec calls would read the rest of this script
echo "$sha" > ~/hagency/.deployed                      # only once it's up (outside @pm's folder)
REMOTE

echo "Checking from here:"
ok=0
python3 deploy/health.py "$ROOMS" "$MATRIX" "$SERVER" || ok=1
# The admin host takes only Cloudflare's addresses: straight at the VM, it must refuse.
ip=$(ssh -G -- "$VM" | awk '$1 == "hostname" {print $2}')
code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 --resolve "$ADMIN:443:$ip" \
  -H "Cf-Access-Jwt-Assertion: not-from-cloudflare" "https://$ADMIN/_synapse/admin/v1/server_version" || true)
if [ "$code" = 403 ]; then echo "✓ $ADMIN refuses requests that don't come through Cloudflare"
else echo "✗ $ADMIN straight at $ip answered $code (expected 403)"; ok=1; fi
exit $ok
