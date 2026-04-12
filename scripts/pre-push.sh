#!/bin/bash
# Pre-push hook — enforces conversation_server.py is only pushed from Tims-Mac-mini.local.
#
# Audit 2026-04-11 §3.3 / §4.1: conversation_server.py existed in three divergent
# copies across two hosts. The canonical host is Tims-Mac-mini.local and only
# changes originating there should be pushed.
#
# This hook inspects the commits being pushed. If any of them touched
# conversation_server.py and we are not on the Mac Mini, the push is refused.
#
# Bypass (emergency only): git push --no-verify

set -u

HOST=$(hostname)
CANONICAL_HOST="Tims-Mac-mini.local"

# Read "local_ref local_sha remote_ref remote_sha" lines from stdin.
TOUCHED=0
while read -r local_ref local_sha remote_ref remote_sha; do
  if [ "$local_sha" = "0000000000000000000000000000000000000000" ]; then
    # Deletion — nothing to check.
    continue
  fi
  if [ "$remote_sha" = "0000000000000000000000000000000000000000" ]; then
    # New branch — check every commit reachable from local_sha that's not on origin yet.
    RANGE="$local_sha"
  else
    RANGE="$remote_sha..$local_sha"
  fi

  if git diff --name-only "$RANGE" -- conversation_server.py 2>/dev/null | grep -q '.'; then
    TOUCHED=1
    break
  fi
done

if [ "$TOUCHED" -eq 1 ] && [ "$HOST" != "$CANONICAL_HOST" ]; then
  cat >&2 <<EOF
BLOCKED: push touches conversation_server.py but current host is '$HOST'.

conversation_server.py is canonical on '$CANONICAL_HOST' only. Pushing changes
to it from any other host risks reintroducing the drift that audit 2026-04-11
§3.3 eliminated.

Fix options:
  1. SSH to $CANONICAL_HOST and push from there.
  2. If this is a legitimate emergency fix, bypass with:
       git push --no-verify
     and immediately rsync the file back to Mac Mini to resync state.
EOF
  exit 1
fi

exit 0
