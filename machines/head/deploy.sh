#!/usr/bin/env bash
# Deploy this repo's `head` config to the DataCrunch orchestrator node.
# Kept as a script because the raw nixos-rebuild invocation is long enough that
# copy-pasting it out of a terminal tends to pick up stray characters.
#
#   ~/nixos-config/machines/head/deploy.sh [host]
#
set -euo pipefail

HOST="${1:-31.56.109.100}"
KEY="${DC_SSH_KEY:-$HOME/.ssh/datacrunch_ed25519}"
FLAKE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

[ -r "$KEY" ] || { echo "deploy: ssh key not found: $KEY" >&2; exit 3; }

echo "deploy: $FLAKE#head -> root@$HOST"
export NIX_SSHOPTS="-i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
exec nixos-rebuild switch --flake "$FLAKE#head" --target-host "root@$HOST"
