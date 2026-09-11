#!/bin/bash
#############################################################
#  Deploy StorePortal — commit everything in this folder and push it to
#  github.com/bgtroy2026/StorePortal. GitHub Actions then builds and publishes.
#
#  Double-click "Deploy StorePortal.command" (runs in Terminal) or run:
#     bash _deploy/deploy.sh "commit message"
#
#  Uses the same GitHub token the Sales Portal deploy stores in your Keychain
#  (service BigGrovePortalDeploy). If none is found it asks once and saves it.
#############################################################
set -u
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="bgtroy2026/StorePortal"
GH_USER="bgtroy2026"
KC_SERVICE="BigGrovePortalDeploy"
ACTIONS_URL="https://github.com/$REPO/actions"
LOG="$PROJECT/_deploy/deploy_last.log"
mkdir -p "$PROJECT/_deploy"
exec > >(tee "$LOG") 2>&1
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

say()  { printf '\n\033[1;32m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*"; read -n1 -s -r -p "Press any key to close..."; echo; exit 1; }

echo "==================================================="
echo "Deploy StorePortal  $(date '+%Y-%m-%d %H:%M:%S')"
echo "==================================================="
cd "$PROJECT" || die "Cannot open $PROJECT"
[ -d .git ] || die "This folder is not a git repository."
git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$REPO.git"

# ---------------------------------------------------------- token (Keychain)
TOKEN=$(security find-generic-password -s "$KC_SERVICE" -a "$GH_USER" -w 2>/dev/null || true)
if [ -z "$TOKEN" ]; then
  echo
  echo "No GitHub token found in your Keychain for $GH_USER."
  echo "Create a classic personal access token with the 'repo' scope at https://github.com/settings/tokens"
  read -r -s -p "Paste the token (it will be saved to your Keychain): " TOKEN; echo
  [ -z "$TOKEN" ] && die "No token entered."
  security add-generic-password -U -s "$KC_SERVICE" -a "$GH_USER" -w "$TOKEN" 2>/dev/null || echo "(could not save to Keychain — continuing)"
fi
AUTH_URL="https://${GH_USER}:${TOKEN}@github.com/${REPO}.git"

# ---------------------------------------------------------- safety: never publish data or secrets
for bad in raw state _site site/data .env bundle_keys.json; do
  if git ls-files --error-unmatch "$bad" >/dev/null 2>&1; then die "'$bad' is tracked by git — it must stay ignored. Nothing was pushed."; fi
done

# ---------------------------------------------------------- commit
MSG="${1:-Update StorePortal $(date '+%Y-%m-%d %H:%M')}"
git add -A
if git diff --cached --quiet; then
  echo "Nothing new to commit."
else
  git -c user.name="Troy Myler" -c user.email="troy@biggrovebrewery.com" commit -q -m "$MSG" || die "Commit failed."
  say "Committed: $MSG"
fi

# ---------------------------------------------------------- reconcile with GitHub (first push merges the README-only initial commit)
GIT_TERMINAL_PROMPT=0 git fetch -q "$AUTH_URL" main 2>/dev/null && HAVE_REMOTE=1 || HAVE_REMOTE=0
if [ "$HAVE_REMOTE" = "1" ]; then
  if ! git merge-base HEAD FETCH_HEAD >/dev/null 2>&1; then
    echo "GitHub has unrelated history (the repo's initial commit) — merging it in, keeping our files."
    git -c user.name="Troy Myler" -c user.email="troy@biggrovebrewery.com" merge -q --allow-unrelated-histories -X ours FETCH_HEAD -m "Merge GitHub initial commit" || die "Merge failed — resolve in the folder and rerun."
  elif ! git merge-base --is-ancestor FETCH_HEAD HEAD; then
    echo "GitHub has newer commits — rebasing ours on top."
    git -c user.name="Troy Myler" -c user.email="troy@biggrovebrewery.com" rebase -q FETCH_HEAD || die "Rebase failed — resolve in the folder and rerun."
  fi
fi

# ---------------------------------------------------------- push
say "Pushing to GitHub..."
OUT=$(GIT_TERMINAL_PROMPT=0 git push "$AUTH_URL" HEAD:main 2>&1); RC=$?
echo "${OUT//$TOKEN/********}"
if [ $RC -ne 0 ]; then
  if echo "$OUT" | grep -qiE 'denied|authentication|403|401'; then
    security delete-generic-password -s "$KC_SERVICE" -a "$GH_USER" >/dev/null 2>&1
    die "GitHub rejected the token (it was removed from the Keychain — run this again and paste a token with 'repo' scope)."
  fi
  die "Push failed — see above."
fi
git branch -q --set-upstream-to=origin/main main 2>/dev/null || true
say "Done. GitHub Actions will build and publish: $ACTIONS_URL"
[ "${2:-}" = "--quiet" ] || { read -n1 -s -r -p "Press any key to close..."; echo; }
