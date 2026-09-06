#!/usr/bin/env bash
# Explicit, resumable provisioning for the full RFAA MSA/template databases.
set -euo pipefail
exec "${RFAA_PYTHON:-python3}" "$(dirname "${BASH_SOURCE[0]}")/databases.py" "$@"
