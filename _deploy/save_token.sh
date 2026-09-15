#!/bin/bash
#############################################################
#  Save the StorePortal deploy token.
#
#  Writes a fine-grained GitHub PAT to ~/Documents/Claude/Projects/.storeportal-token so the Cowork VM
#  (Claude's shell) can push. Deliberately ONE LEVEL ABOVE the repo, so no `git add -A` can stage it.
#
#  The token is read with `read -s` — it is never shown on screen, never echoed, and never written anywhere
#  except that one file, which is created with owner-only permissions.
#
#  Your Keychain token and "Deploy StorePortal.command" are untouched and keep working.
#############################################################
set -u
DEST="$HOME/Documents/Claude/Projects/.storeportal-token"

echo "==================================================="
echo "Save StorePortal deploy token"
echo "==================================================="
echo
echo "Create the token first at:"
echo "  https://github.com/settings/tokens  ->  Fine-grained tokens"
echo
echo "  Repository access : Only select repositories -> bgtroy2026/StorePortal"
echo "  Permissions       : Contents   Read and write   (to push)"
echo "                      Workflows  Read and write   (to push .github/workflows changes)"
echo "                      Actions    Read and write   (optional: lets Claude start a build)"
echo
read -r -s -p "Paste the token (input is hidden), then press Return: " TOKEN; echo
TOKEN="$(printf '%s' "$TOKEN" | tr -d ' \t\r\n')"

if [ -z "$TOKEN" ]; then
  echo "Nothing entered — no file written."
elif [ ${#TOKEN} -lt 20 ]; then
  echo "That looks too short to be a GitHub token — no file written."
else
  mkdir -p "$(dirname "$DEST")"
  ( umask 177; printf '%s\n' "$TOKEN" > "$DEST" )   # umask 177 => created as 0600, never briefly world-readable
  chmod 600 "$DEST"
  echo
  echo "Saved to: $DEST"
  ls -l "$DEST"
  echo
  echo "Claude can now deploy with:  bash _deploy/deploy_vm.sh \"message\""
  echo "To revoke later: delete this file, and revoke the token on GitHub."
fi
echo
read -n1 -s -r -p "Press any key to close..."; echo
