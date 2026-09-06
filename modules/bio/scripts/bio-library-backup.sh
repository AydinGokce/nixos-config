# Pull a consistent verified snapshot; retain history and test a local restore.
set -euo pipefail
[ ! -r "${BIO_CONFIG_FILE:-/etc/bio/config.sh}" ] || source "${BIO_CONFIG_FILE:-/etc/bio/config.sh}"
exec python3.12 "${BIO_LIBRARY_CLIENT:-/etc/bio/py/library_client.py}" backup "$@"
