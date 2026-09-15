#!/bin/bash
#############################################################
#  Deploy StorePortal from the Cowork Linux VM (Claude's shell).
#
#  Same job as _deploy/deploy.sh, which stays the canonical, human path: that one reads the token from the
#  macOS Keychain and is what Troy double-clicks. This one exists only because the VM is a different machine
#  and cannot reach the Keychain, so it reads a scoped token from a file instead.
#
#  The token file lives OUTSIDE the repository on purpose — one directory up, in the connected folder root —
#  so that no `git add -A` anywhere in this repo can ever stage it.
#
#  Token: a fine-grained PAT limited to bgtroy2026/StorePortal with
#     Contents:  Read and write   (to push)
#     Workflows: Read and write   (to push changes to .github/workflows/*)
#     Actions:   Read and write   (optional — lets this script also start the build)
#
#  Usage:  bash _deploy/deploy_vm.sh "commit message"
#############################################################
set -u
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="bgtroy2026/StorePortal"
GH_USER="bgtroy2026"
TOKEN_FILE="${STOREPORTAL_TOKEN_FILE:-$(cd "$PROJECT/.." && pwd)/.storeportal-token}"

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
cd "$PROJECT" || die "Cannot open $PROJECT"
[ -d .git ] || die "This folder is not a git repository."

# ---------------------------------------------------------- token
[ -f "$TOKEN_FILE" ] || die "No token file at $TOKEN_FILE — see the header of this script."
TOKEN="$(tr -d ' \t\r\n' < "$TOKEN_FILE")"
[ -n "$TOKEN" ] && [ ${#TOKEN} -ge 20 ] || die "Token file is empty or too short to be a token."
mask() { sed -e "s/${TOKEN}/********/g"; }          # nothing from here on may print the token
AUTH_URL="https://${GH_USER}:${TOKEN}@github.com/${REPO}.git"

# ---------------------------------------------------------- safety: never publish data, secrets, or the token
for bad in raw state _site site/data .env bundle_keys.json; do
  git ls-files --error-unmatch "$bad" >/dev/null 2>&1 && die "'$bad' is tracked by git — it must stay ignored. Nothing was pushed."
done
case "$TOKEN_FILE" in
  "$PROJECT"/*) die "The token file is inside the repository. Move it out before deploying." ;;
esac

# ---------------------------------------------------------- commit
MSG="${1:-Update StorePortal $(date '+%Y-%m-%d %H:%M')}"
git add -A
if git diff --cached --quiet; then
  echo "Nothing new to commit."
else
  git -c user.name="Troy Myler" -c user.email="troy@biggrovebrewery.com" \
      commit -q -m "$MSG" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" \
      -m "Claude-Session: https://claude.ai/code/session_01PSTYkonDt6dVkxJ1PPBPLL" || die "Commit failed."
  echo "Committed: $MSG"
fi

# ---------------------------------------------------------- reconcile, then push
if GIT_TERMINAL_PROMPT=0 git fetch -q "$AUTH_URL" main 2>/dev/null; then
  if ! git merge-base --is-ancestor FETCH_HEAD HEAD 2>/dev/null; then
    echo "GitHub has newer commits — rebasing ours on top."
    git -c user.name="Troy Myler" -c user.email="troy@biggrovebrewery.com" rebase -q FETCH_HEAD \
      || die "Rebase failed — resolve in the folder and rerun."
  fi
fi

OUT=$(GIT_TERMINAL_PROMPT=0 git push "$AUTH_URL" HEAD:main 2>&1); RC=$?
echo "$OUT" | mask
if [ $RC -ne 0 ]; then
  echo "$OUT" | grep -qiE 'workflow|refusing to allow' && \
    die "The token lacks the Workflows permission and this push touches .github/workflows/. Add it to the token."
  echo "$OUT" | grep -qiE 'denied|authentication|403|401' && \
    die "GitHub rejected the token — check it has Contents: Read and write on $REPO and has not expired."
  die "Push failed — see above."
fi
SHA=$(git rev-parse --short HEAD)
echo "Pushed $SHA to $REPO"

# ---------------------------------------------------------- optionally start the build
# Only if the token happens to carry the Actions permission. A 403 here is not a failure: the push is what
# matters, and the run can always be started from the Actions page instead.
if [ "${2:-}" = "--run" ]; then
  CODE=$(curl -sS -o /tmp/dispatch.out -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$REPO/actions/workflows/refresh.yml/dispatches" \
    -d '{"ref":"main","inputs":{}}' 2>&1)
  if [ "$CODE" = "204" ]; then echo "Started a Nightly refresh run."
  else echo "Could not start the run (HTTP $CODE) — start it from https://github.com/$REPO/actions"; fi
fi
