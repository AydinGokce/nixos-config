# Manage private sequence preparation on temporary compute with persistent data.
set -euo pipefail
case "${1:---help}" in
  install|convert|prepare|serve)
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
bio-msa convert [--worker TYPE --timeout SECONDS]
bio-msa prepare --model openfold3|boltz2|protenix --fasta FILE [--worker TYPE]
bio-msa serve [--worker TYPE --timeout SECONDS]
bio-msa status
bio-msa compare --help

Install, preparation and serving use temporary high-memory compute. Conversion
defaults to CPU.16V.64G and creates sequence databases without full search indexes;
it does not make the databases ready for preparation. Database volumes and
prepared inputs remain after compute is removed. To prepare then predict:
  bio-submit openfold3 --fasta FILE --msa-backend private
Public MSA remains the default until prediction-quality comparisons pass.
HELP
    ;;
  *) echo 'bio-msa: unknown command; use --help' >&2; exit 2 ;;
esac
