# Manage the authoritative construct library on the configured cloud head.
set -euo pipefail
[ ! -r "${BIO_CONFIG_FILE:-/etc/bio/config.sh}" ] || source "${BIO_CONFIG_FILE:-/etc/bio/config.sh}"
exec python3.12 "${BIO_LIBRARY_CLIENT:-/etc/bio/py/library_client.py}" "$@"
