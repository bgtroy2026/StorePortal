#!/bin/bash
# Run ONCE on the Mac. Makes a random upload key, keeps it outside the project folders, and puts it on the
# clipboard so it can be pasted into the Apps Script as the script property DEPLETIONS_PUT_KEY.
CFG="$HOME/Library/Application Support/BigGroveDeploy"
KEY="$CFG/depletions_put.key"
mkdir -p "$CFG"
if [ ! -s "$KEY" ]; then
  umask 077
  /usr/bin/openssl rand -base64 36 | tr -d '\n' > "$KEY"
fi
chmod 600 "$KEY"
/usr/bin/pbcopy < "$KEY"
cat <<'MSG'

  The upload key is on your clipboard (it is not shown here on purpose).

  1. Open the "Store Director Login" Apps Script  ->  Project Settings (gear)  ->  Script properties
  2. Add script property     name:  DEPLETIONS_PUT_KEY     value:  paste
  3. Save script properties.

  No redeploy is needed. From then on every Sales Portal deploy refreshes the depletions in the
  Store Director Portal on its own.

MSG
read -n1 -s -p "Press any key when that's done (this clears the clipboard)..."
printf '' | /usr/bin/pbcopy
echo
