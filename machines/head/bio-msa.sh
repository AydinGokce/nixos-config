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
bio-msa session start [--provider aws|verda --worker TYPE --timeout SECONDS]
bio-msa session status
bio-msa session stop
bio-msa prepare --model openfold3|boltz2|protenix --fasta FILE [--require-session]
bio-msa prepare --model rf3 --json CHAIN_QUERIES.json [--require-session]
bio-msa install [--json MANIFEST.json --worker TYPE --timeout SECONDS --spot]
bio-msa convert [--worker TYPE --timeout SECONDS]
bio-msa panel --json MANIFEST.json [--worker TYPE --timeout SECONDS --spot]
bio-msa serve [--worker TYPE --timeout SECONDS]
bio-msa status
bio-msa compare --help

BIO_MSA_PROVIDER=aws routes shared session/prepare to the managed AWS CPU pool
in us-east-1. It requires resident-768gib-v1, full index prefetch, 16 native
threads and on-demand compute. Normal limits are two hours maximum and 15 minutes
idle. Idle shutdown STOPS the exact EC2 instance and retains its OS/database
disks; the next start reloads RAM. Persistent disks continue billing while stopped.
One-time database/runtime copying uses a separately bounded bio-aws-msa prepare
operation under an exact supervised head unit; it is not a native search session.

Private preparation joins one shared session, starts it when needed, and waits
for verified readiness. --require-session disables automatic startup. Existing
prepared inputs need no worker. Private never falls back to a public endpoint.
Completed AWS outputs are copied to the head before original native validation.
Uncertain transfers or launch replies retain their exact request for recovery.

The production profile default is resident-768gib-v1 for Verda too. The explicit
--search-profile mapped-128gb-v1 experiment is UNQUALIFIED: the large-editor NFS
trial hit the native one-hour timeout. It uses 4 threads, a 96 GiB cap, no swap
and --warm report, with complete indexes paged on demand. AWS rejects that profile.
An existing session retains its original provider/profile and is not reconfigured.

install/convert/panel/serve remain standalone Verda operator routes even when
BIO_MSA_PROVIDER=aws. Resident install/search requires 768 GiB available RAM.
Conversion defaults to CPU.16V.64G and creates databases without full search indexes;
it does not make the databases ready for preparation.
FIN-02 selection enforces supported hosts/images, a $13/hour instance ceiling,
current quotes and the combined gross $1000 AWS+Verda project guard. Credits do
not reduce counted spending. --spot applies only to Verda. An AWS --worker
override must match r6a.32xlarge; neither bypasses budget or guest validation.

Verda selection may wait up to two hours with 30-second pauses before renting.
--capacity-wait-seconds 0..7200 on session start/prepare changes that window
(0 checks once). Waiting never extends the paid worker or native search limits.
A confirmed pre-allocation failure can recover on a later request; uncertain
allocations keep their registration. The native API limits each ticket to one hour.

To prepare then predict:
  bio-submit openfold3 --fasta FILE --msa-backend private
GC Protein Engineering Console defaults to private MSA; low-level bio-submit
keeps its public default. Verda GPU prediction workers have separate lifetimes.
Standalone panel runs targets serially without inference and retains failures.
install --json prepares its full panel on the same worker within the original
timeout. Plain install only installs databases.

HELP
    ;;
  *) echo 'bio-msa: unknown command; use --help' >&2; exit 2 ;;
esac
