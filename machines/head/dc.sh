# dc — budget-aware DataCrunch/Verda instance CLI. Python owns the locked cost
# ledger, launch reservations, confirmed teardown and watchdog.
set -euo pipefail
# Python applies this only to launch/run, outside its accounting lock. Cleanup
# and watchdog commands remain immediate even when a launch is cooling down.
export DC_LAUNCH_COOLDOWN_SECONDS="${DC_LAUNCH_COOLDOWN_SECONDS-0}"
if [ "${1:-}" != --help ] && [ "${1:-}" != -h ]; then
  CRED="${DC_CREDENTIALS_FILE:-/root/.config/datacrunch/credentials.env}"
  [ -r "$CRED" ] || { echo "dc: missing credentials file: $CRED" >&2; exit 3; }
  # shellcheck source=/dev/null
  source "$CRED"
  export DATACRUNCH_CLIENT_ID DATACRUNCH_CLIENT_SECRET
fi
exec python3 "${DC_HELPER:-/etc/bio-tools/dc-budget.py}" "$@"
