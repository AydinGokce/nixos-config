# Manage private sequence preparation on temporary compute with persistent data.
set -euo pipefail
case "${1:---help}" in
  session)
    shift
    exec python3 "${BIO_TOOLS_SRC:-/etc/bio-tools}/msa/session_client.py" "$@" ;;
  prepare)
    shift
    exec python3 "${BIO_TOOLS_SRC:-/etc/bio-tools}/msa/session_client.py" prepare "$@" ;;
  install|convert|panel|serve)
    command="$1"; shift
    exec bio-submit msa --sub "$command" "$@" ;;
  status)
    dc spend
    bio-msa-storage check
    exec python3 /etc/bio-tools/msa/databases.py validate --root /mnt/bio-msa-databases/colabfold ;;
  compare)
    shift; exec python3 /etc/bio-tools/msa/prepared.py compare "$@" ;;
  --help|-h)
    cat <<'HELP'
bio-msa install [--worker TYPE --timeout SECONDS]
bio-msa install --json MANIFEST.json [--worker TYPE --timeout SECONDS --spot]
bio-msa convert [--worker TYPE --timeout SECONDS]
bio-msa panel --json MANIFEST.json [--worker TYPE --timeout SECONDS --spot]
bio-msa session start [--worker TYPE --timeout SECONDS --idle-seconds SECONDS --warm report|prefetch|lock]
bio-msa session status
bio-msa session stop
bio-msa prepare --model openfold3|boltz2|protenix --fasta FILE
bio-msa prepare --model rf3 --json CHAIN_QUERIES.json
bio-msa serve [--worker TYPE --timeout SECONDS]
bio-msa status
bio-msa compare --help

Private preparation reuses an explicitly started, budgeted session. It never
rents a new worker per request or falls back to a public endpoint. A session
keeps its full database API alive across requests until idle or maximum lifetime;
the database volume persists after its managed worker is removed. Index prefetch
is the default; report only measures residency, while lock requires sufficient
RAM headroom and a suitable memory-lock limit. Readiness records the actual mode.

Installation and standalone panel/serving use temporary high-memory compute. Conversion
defaults to CPU.16V.64G and creates sequence databases without full search indexes;
it does not make the databases ready for preparation. Database volumes and
prepared inputs remain after compute is removed. To prepare then predict:
  bio-submit openfold3 --fasta FILE --msa-backend private
Public MSA remains the default until prediction-quality comparisons pass.
Session start, installation and standalone serving select FIN-02 compute with at least 768 GiB RAM and a
$13/hour instance-price ceiling, including spot capacity. --spot selects only
spot offers; --worker TYPE overrides automatic selection. dc rechecks the quote
and total project budget before launch. Conversion keeps its smaller CPU default.
Panel preparation keeps one private API worker for all manifest targets, runs
them serially without inference, and records failures without dropping targets.
Install with --json prepares that panel after installation on the same worker,
using the remaining original timeout. Plain install only installs databases.
HELP
    ;;
  *) echo 'bio-msa: unknown command; use --help' >&2; exit 2 ;;
esac
