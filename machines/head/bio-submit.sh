# Run a model on a temporary GPU and fetch results before deleting the instance.
set -euo pipefail
SHARED_NFS="nfs.fin-02.datacrunch.io:/bio-shared-G523CVN6KYMH"
SHARED_VOL="b8b3b446-e464-44dd-9e01-6402489f8c5a"
SHARED_MNT=${BIO_SHARED_MNT:-/mnt/bio-shared}
TOOLS_SRC=${BIO_TOOLS_SRC:-/etc/bio-tools}
STATE_DIR=${BIO_STATE_DIR:-/var/lib/dc}
RESULTS_DIR=${BIO_RESULTS_DIR:-/var/lib/bio-runs}
LOC=FIN-02
[ ! -r "${BIO_CLUSTER_CONFIG:-/etc/bio-tools/cluster.sh}" ] || source "${BIO_CLUSTER_CONFIG:-/etc/bio-tools/cluster.sh}"
usage() { cat <<'USAGE'
bio-submit MODEL --fasta FILE [options]
Models: boltz2, openfold3, protenix, rfaa, rfdiffusion, mpnn, esm, evolvepro
Inputs: --fasta FILE | --pdb FILE | --json FILE | --contigs '[50-50]'
Options: --labels CSV (EVOLVEpro), --sub CMD, --model NAME, --num N,
         --gpu TYPE, --spot, --timeout SECONDS (default 7200), -- EXTRA_ARGS
RFAA: --sub full (default) or --sub single-seq; full needs bio-rfaa-databases.
EVOLVEpro: --sub rank (default) or --sub embed; --labels measured.csv for ranking.
Results: /var/lib/bio-runs/JOB on the head, including run.log and job.json.
USAGE
}
tool="${1:-}"; [ $# -eq 0 ] || shift
case "$tool" in
  esm|esm2) recipe=esm; inkind=fasta; tier=modern ;;
  mpnn|proteinmpnn) recipe=mpnn; inkind=pdb; tier=modern ;;
  rfdiffusion|rfd) recipe=rfdiffusion; inkind=optpdb; tier=ampere ;;
  rfaa) recipe=rfaa; inkind=fasta; tier=ampere ;;
  evolvepro) recipe=evolvepro; inkind=fasta; tier=modern ;;
  boltz2|boltz) recipe=boltz2; inkind=fasta; tier=latest ;;
  protenix) recipe=protenix; inkind=fasta; tier=latest ;;
  openfold3|of3) recipe=openfold3; inkind=fasta; tier=latest ;;
  af3|alphafold3) recipe=af3; inkind=json; tier=latest ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
gpu=""; spot=""; infile=""; labels=""; model=""; sub=""; contigs=""; num=""; temp=""; name=""; seconds=7200; extra=()
if [[ "$recipe" = esm || "$recipe" = evolvepro ]]; then
  case "${1:-}" in ""|-*) ;; *) sub="$1"; shift ;; esac
fi
while [ $# -gt 0 ]; do
  case "$1" in
    --gpu|--model|--fasta|--pdb|--json|--in|--input-pdb|--labels|--sub|--contigs|--num-designs|--num|--num-seqs|--temp|--name|--timeout)
      [ $# -ge 2 ] || { echo "bio-submit: $1 needs a value" >&2; exit 2; }
      case "$1" in
        --gpu) gpu="$2";; --model) model="$2";; --labels) labels="$2";;
        --sub) sub="$2";; --contigs) contigs="$2";; --temp) temp="$2";;
        --name) name="$2";; --timeout) seconds="$2";;
        --num-designs|--num|--num-seqs) num="$2";; *) infile="$2";;
      esac; shift ;;
    --spot) spot=--spot ;;
    --) shift; extra+=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) echo "bio-submit: unknown option $1 (put model-specific arguments after --)" >&2; exit 2 ;;
  esac
  shift
done
[[ "$seconds" =~ ^[0-9]+$ ]] && (( seconds >= 60 && seconds <= 85500 )) || { echo 'bio-submit: timeout must be 60..85500 seconds' >&2; exit 2; }
[ -z "$infile" ] || [ -f "$infile" ] || { echo "bio-submit: input not found: $infile" >&2; exit 2; }
[ -z "$labels" ] || [ -f "$labels" ] || { echo "bio-submit: labels not found: $labels" >&2; exit 2; }
[ -n "$infile" ] || [ "$inkind" = optpdb ] || { echo 'bio-submit: input required' >&2; exit 2; }
[ "$recipe" != rfdiffusion ] || [ -n "$contigs" ] || { echo 'bio-submit: --contigs required' >&2; exit 2; }
db_nfs=""; volumes=(--volume "$SHARED_VOL")
if [ "$recipe" = rfaa ]; then
  for setting in RFAA_CPU RFAA_MEM_GB; do
    [[ -z "${!setting:-}" || "${!setting}" =~ ^[1-9][0-9]*$ ]] \
      || { echo "bio-submit: $setting must be a positive integer" >&2; exit 2; }
  done
  python3 "$TOOLS_SRC/rfaa/prepare.py" --fasta "$infile" --validate-only
  case "${sub:-full}" in
    full)
      [ -n "${RFAA_DB_VOLUME:-}" ] && [ -n "${RFAA_DB_NFS:-}" ] \
        || { echo 'bio-submit: full RFAA needs the database volume configured in rfaa-storage.nix' >&2; exit 2; }
      db_nfs="$RFAA_DB_NFS"; volumes+=(--volume "$RFAA_DB_VOLUME") ;;
    single-seq) ;;
    *) echo 'bio-submit: RFAA --sub must be full or single-seq' >&2; exit 2 ;;
  esac
fi
if [ "$recipe" = evolvepro ]; then
  for ((i=0; i<${#extra[@]}; i+=2)); do
    [[ "${extra[$i]}" = --regressor || "${extra[$i]}" = --seed ]] && (( i+1 < ${#extra[@]} )) \
      || { echo 'bio-submit: EVOLVEpro extras are --regressor NAME and --seed N' >&2; exit 2; }
  done
  check=(--validate-only --input "$infile" --sub "${sub:-rank}" --num "${num:-12}")
  [ -z "$labels" ] || check+=(--labels "$labels")
  python3 "$TOOLS_SRC/py/evolvepro_cloud.py" "${check[@]}" "${extra[@]}"
fi
# Serialize jobs while environments and provider volume attachment are shared.
# The lock is independent of dc's accounting lock.
mkdir -p "$STATE_DIR" "$RESULTS_DIR"
exec 9>"$STATE_DIR/bio-submit.lock"
flock 9
jobid="$recipe-$(date -u +%Y%m%d-%H%M%S)-$$"
LOCALOUT="$RESULTS_DIR/$jobid"; mkdir -p "$LOCALOUT"
exec > >(tee -a "$LOCALOUT/run.log") 2>&1
run="$SHARED_MNT/runs/$jobid"; mkdir -p "$run/in" "$run/out"
RIN=""; RLABELS=""
if [ -n "$infile" ]; then cp "$infile" "$run/in/input.${infile##*.}"; RIN="$run/in/input.${infile##*.}"; fi
if [ -n "$labels" ]; then cp "$labels" "$run/in/labels.csv"; RLABELS="$run/in/labels.csv"; fi
ROUT="$run/out"
mkdir -p "$SHARED_MNT/tools"
rsync -aL --delete "$TOOLS_SRC/" "$SHARED_MNT/tools/"
# printf %q preserves argument boundaries and prevents input text becoming code.
remote_file="$LOCALOUT/remote.sh"
{
  printf 'set -euo pipefail\n'
  printf 'export IN=%q OUT=%q LABELS=%q MODEL=%q SUB=%q CONTIGS=%q NUM=%q TEMP=%q NAME=%q\n' "$RIN" "$ROUT" "$RLABELS" "$model" "$sub" "$contigs" "$num" "$temp" "${name:-$recipe}"
  printf 'EXTRA_ARGS=('
  if [ "${#extra[@]}" -gt 0 ]; then printf ' %q' "${extra[@]}"; fi
  printf ' )\n'
  printf 'export EXTRA=%q\n' "${extra[*]:-}"
  printf 'SHARED_NFS=%q\n' "$SHARED_NFS"
  printf 'RFAA_DB_NFS=%q\n' "$db_nfs"
  printf 'export RFAA_DB_DIR=%q\n' "${RFAA_DB_DIR:-/mnt/bio-databases/rfaa}"
  printf 'export RFAA_CPU=%q RFAA_MEM_GB=%q\n' "${RFAA_CPU:-4}" "${RFAA_MEM_GB:-64}"
  cat <<'REMOTE'
export HOME=/root PATH=/root/.local/bin:$PATH
need=()
for pair in 'mount.nfs:nfs-common' 'curl:curl' 'git:git' 'rsync:rsync' 'wget:wget' 'gcc:build-essential' 'bzip2:bzip2'; do
  command -v "${pair%%:*}" >/dev/null 2>&1 || need+=("${pair#*:}")
done
# A compiler can be installed without Python.h. Check headers independently.
python3 -c 'import pathlib,sysconfig; assert (pathlib.Path(sysconfig.get_path("include"))/"Python.h").is_file()' >/dev/null 2>&1 || need+=(python3-dev)
if [ "${#need[@]}" -gt 0 ]; then
  # sshd can be ready while the image's first-boot package setup still owns
  # apt's lists lock. The dpkg timeout alone does not cover apt-get update.
  apt_retry() {
    local attempt
    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
      if sudo env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=180 "$@"; then
        return 0
      fi
      [ "$attempt" -lt 12 ] || return 1
      echo "[bio-submit] package setup not ready, retry $attempt/12"
      sleep 10
    done
  }
  apt_retry update -qq
  apt_retry install -y -qq "${need[@]}"
fi
sudo mkdir -p /mnt/bio-shared
for i in 1 2 3 4 5 6 7 8; do
  mountpoint -q /mnt/bio-shared && break
  sudo mount -t nfs -o nconnect=16,nolock "$SHARED_NFS" /mnt/bio-shared && break
  echo "[bio-submit] shared FS not ready, attempt $i/8"; sleep 10
done
mountpoint -q /mnt/bio-shared || { echo 'shared FS never mounted' >&2; exit 1; }
if [ -n "$RFAA_DB_NFS" ]; then
  sudo mkdir -p /mnt/bio-databases
  for i in 1 2 3 4 5 6 7 8; do
    mountpoint -q /mnt/bio-databases && break
    sudo mount -t nfs -o nconnect=16,nolock,ro "$RFAA_DB_NFS" /mnt/bio-databases && break
    sleep 10
  done
  mountpoint -q /mnt/bio-databases || { echo 'RFAA database volume never mounted' >&2; exit 1; }
fi
command -v uv >/dev/null 2>&1 || curl --fail -LsS https://astral.sh/uv/install.sh | sh
mkdir -p "$OUT"
source /mnt/bio-shared/tools/recipes/_common.sh
REMOTE
  printf 'source %q\n' "/mnt/bio-shared/tools/recipes/$recipe.sh"
  printf 'sync\n'
} > "$remote_file"
id=""; ip=""
cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  if [ -n "$id" ]; then
    echo "bio-submit: removing $id"
    dc rm "$id" || { echo "bio-submit: ERROR deleting $id; inspect dc ls" >&2; status=1; }
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
# Older CUDA wheels do not support Blackwell. CUDA 11 RF stacks use Ampere.
if [ -n "$gpu" ]; then candidates=("$gpu")
else
  case "$tier" in
    ampere) candidates=(1A100.22V 1A6000.10V 1A100.40S.22V) ;;
    modern) candidates=(1A100.22V 1L40S.20V 1H100.80S.32V 1A6000.10V) ;;
    latest) candidates=(1A100.22V 1L40S.20V 1RTXPRO6000.30V 1H100.80S.32V) ;;
  esac
fi
for g in "${candidates[@]}"; do
  echo "bio-submit: launching $g ..."
  max_hours=$(python3 -c 'import sys; print((int(sys.argv[1])+900)/3600)' "$seconds")
  image_args=()
  [ "$g" != 1A6000.10V ] || image_args=(--image ubuntu-24.04-cuda-12.6-docker)
  if out=$(dc launch "$g" --loc "$LOC" ${spot:+"$spot"} "${volumes[@]}" "${image_args[@]}" --max-hours "$max_hours" 2>&1); then
    id=$(printf '%s\n' "$out" | sed -n 's/.*READY id=\([^ ]*\).*/\1/p' | tail -1)
    ip=$(printf '%s\n' "$out" | sed -n 's/.*READY.*ip=\([^ ]*\).*/\1/p' | tail -1)
    [ -n "$id" ] && [ -n "$ip" ] && break
    echo "$out"; echo 'bio-submit: malformed launch response' >&2; exit 1
  else
    status=$?; printf '%s\n' "$out" | tail -3
    [ "$status" -ne 4 ] || exit 4
  fi
done
[ -n "$id" ] || { echo 'bio-submit: no compatible GPU capacity' >&2; exit 5; }
SSHO=(-i /root/.ssh/datacrunch_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3)
ready=0
for _ in $(seq 1 30); do
  if ssh "${SSHO[@]}" "root@$ip" true 2>/dev/null; then ready=1; break; fi
  sleep 8
done
[ "$ready" = 1 ] || { echo 'bio-submit: sshd never became ready' >&2; exit 1; }
python3 - "$LOCALOUT/job.json" "$jobid" "$recipe" "$id" "$ip" "$g" "$seconds" <<'PY'
import json,sys,datetime
p,job,model,instance,ip,gpu,timeout=sys.argv[1:]
with open(p,'w') as f: json.dump(dict(job=job,model=model,instance=instance,ip=ip,gpu=gpu,timeout=int(timeout),started=datetime.datetime.now(datetime.timezone.utc).isoformat()),f,indent=2)
PY
echo "bio-submit: running $recipe on $id ($ip), timeout ${seconds}s"
status=0
timeout --signal=TERM --kill-after=60 "$seconds" ssh "${SSHO[@]}" "root@$ip" bash -s < "$remote_file" || status=$?
# Fetch partial outputs even on failure; model status must remain nonzero.
echo "bio-submit: fetching results -> $LOCALOUT"
rsync -a -e "ssh ${SSHO[*]}" "root@$ip:$ROUT/" "$LOCALOUT/" || { echo 'bio-submit: result retrieval failed' >&2; status=1; }
python3 - "$LOCALOUT/job.json" "$status" <<'PY'
import json,sys,datetime
p,status=sys.argv[1:]
with open(p) as f: data=json.load(f)
data.update(exit_status=int(status),finished=datetime.datetime.now(datetime.timezone.utc).isoformat())
with open(p,'w') as f: json.dump(data,f,indent=2)
PY
[ "$status" -eq 0 ] || { echo "bio-submit: FAILED ($status); logs at $LOCALOUT" >&2; exit "$status"; }
dc rm "$id"; id=""
echo "bio-submit: DONE — results at $LOCALOUT (head-local)"
