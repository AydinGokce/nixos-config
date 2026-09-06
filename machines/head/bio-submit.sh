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
MSA: --msa-backend public|private, or --msa-bundle DIRECTORY for a prepared input.
Database jobs: bio-submit msa --sub install|convert|panel|prepare|serve [--model MODEL --fasta FILE]
Panel preparation: bio-submit msa --sub panel --json MANIFEST.json [--worker TYPE]
Install then prepare: bio-submit msa --sub install --json MANIFEST.json [--worker TYPE]
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
  protenix) recipe=protenix; inkind=fasta; tier=cuda128 ;;
  openfold3|of3) recipe=openfold3; inkind=fasta; tier=latest ;;
  msa) recipe=msa; inkind=optional; tier=msa ;;
  af3|alphafold3) recipe=af3; inkind=json; tier=latest ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
gpu=""; spot=""; infile=""; labels=""; model=""; sub=""; contigs=""; num=""; temp=""; name=""; seconds=7200; extra=()
msa_backend="${BIO_MSA_DEFAULT_BACKEND:-public}"; msa_bundle=""; bundle_result=""
panel_manifest_sha=""
case "$recipe" in openfold3|boltz2|protenix) ;; *) msa_backend=public;; esac
if [[ "$recipe" = esm || "$recipe" = evolvepro ]]; then
  case "${1:-}" in ""|-*) ;; *) sub="$1"; shift ;; esac
fi
while [ $# -gt 0 ]; do
  case "$1" in
    --gpu|--worker|--model|--fasta|--pdb|--json|--in|--input-pdb|--labels|--sub|--contigs|--num-designs|--num|--num-seqs|--temp|--name|--timeout|--msa-backend|--msa-bundle|--bundle-result)
      [ $# -ge 2 ] || { echo "bio-submit: $1 needs a value" >&2; exit 2; }
      case "$1" in
        --gpu|--worker) gpu="$2";; --model) model="$2";; --labels) labels="$2";;
        --msa-backend) msa_backend="$2";; --msa-bundle) msa_bundle="$2";; --bundle-result) bundle_result="$2";;
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
[ -n "$infile" ] || [[ "$inkind" = opt* ]] || { echo 'bio-submit: input required' >&2; exit 2; }
[ "$recipe" != rfdiffusion ] || [ -n "$contigs" ] || { echo 'bio-submit: --contigs required' >&2; exit 2; }
if [ "$recipe" = protenix ] && [ -n "$gpu" ]; then
  case "$gpu" in
    1A100.22V|1A100.40S.22V|1L40S.20V|1H100.80S.32V) ;;
    *) echo 'bio-submit: Protenix requires an A100, L40S or H100 worker with CUDA 12.8; its pinned kernels do not support Blackwell' >&2; exit 2 ;;
  esac
fi
case "$msa_backend" in public|private) ;; *) echo 'bio-submit: --msa-backend must be public or private' >&2; exit 2;; esac
if [ "$recipe" = msa ]; then
  sub="${sub:-prepare}"
  case "$sub" in
    install|convert|serve)
      [ -z "$bundle_result" ] || { echo 'bio-submit: --bundle-result is only valid for MSA preparation' >&2; exit 2; } ;;
    prepare)
      [ -n "$infile" ] || { echo 'bio-submit: MSA preparation needs --fasta' >&2; exit 2; }
      case "$model" in openfold3|boltz2|protenix) ;; *) echo 'bio-submit: MSA preparation needs --model openfold3|boltz2|protenix' >&2; exit 2;; esac ;;
    panel)
      [ -n "$infile" ] || { echo 'bio-submit: MSA panel needs --json MANIFEST.json' >&2; exit 2; } ;;
    *) echo 'bio-submit: MSA --sub must be install, convert, panel, prepare or serve' >&2; exit 2;;
  esac
  if [ "$sub" = panel ] || { [ "$sub" = install ] && [ -n "$infile" ]; }; then
    [ -z "$model$labels$contigs$num$temp$bundle_result" ] && [ "${#extra[@]}" -eq 0 ] \
      || { echo 'bio-submit: panel targets/settings belong in the manifest; model overrides and --bundle-result are unsupported' >&2; exit 2; }
    panel_manifest_sha=$(python3 "$TOOLS_SRC/msa/panel.py" validate --manifest "$infile" --hash-only)
  fi
  [ "$sub" != convert ] || tier=msa_convert
  [ -z "$msa_bundle" ] || { echo 'bio-submit: a database job cannot consume an inference bundle' >&2; exit 2; }
elif [ -n "$bundle_result" ]; then
  echo 'bio-submit: --bundle-result is only valid for MSA preparation' >&2; exit 2
fi
db_nfs=""; db_volume=""; storage_tool=bio-rfaa-storage; volumes=(--volume "$SHARED_VOL")
if [ "$recipe" = rfaa ]; then
  for setting in RFAA_CPU RFAA_MEM_GB; do
    [[ -z "${!setting:-}" || "${!setting}" =~ ^[1-9][0-9]*$ ]] \
      || { echo "bio-submit: $setting must be a positive integer" >&2; exit 2; }
  done
  python3 "$TOOLS_SRC/rfaa/prepare.py" --fasta "$infile" --validate-only
  case "${sub:-full}" in
    full)
      # The default 64 GiB HHsuite limit needs the A100 nodes' 120 GB host RAM.
      # A6000 workers have only 60 GB; keep those for single-sequence jobs.
      tier=ampere_full
      [ -n "${RFAA_DB_VOLUME:-}" ] && [ -n "${RFAA_DB_NFS:-}" ] \
        || { echo 'bio-submit: full RFAA needs the database volume configured in rfaa-storage.nix' >&2; exit 2; }
      bio-rfaa-storage check --volume "$RFAA_DB_VOLUME"
      db_nfs="$RFAA_DB_NFS"; db_volume="$RFAA_DB_VOLUME"; volumes+=(--volume "$db_volume") ;;
    single-seq) ;;
    *) echo 'bio-submit: RFAA --sub must be full or single-seq' >&2; exit 2 ;;
  esac
fi
if [ "$recipe" = msa ]; then
  [ -n "${MSA_DB_VOLUME:-}" ] && [ -n "${MSA_DB_NFS:-}" ] \
    || { echo 'bio-submit: private MSA needs storage configured in msa-storage.nix' >&2; exit 2; }
  storage_tool=bio-msa-storage
  "$storage_tool" check --volume "$MSA_DB_VOLUME"
  db_nfs="$MSA_DB_NFS"; db_volume="$MSA_DB_VOLUME"; volumes+=(--volume "$db_volume")
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
# Prepare and validate complete native inputs before renting an inference GPU.
if [ "$recipe" != msa ] && { [ "$msa_backend" = private ] || [ -n "$msa_bundle" ]; }; then
  case "$recipe" in openfold3|boltz2|protenix) ;; *) echo 'bio-submit: this model does not use the shared MSA backend' >&2; exit 2;; esac
  if [ -z "$msa_bundle" ]; then
    mkdir -p "$STATE_DIR"
    prep_result=$(mktemp "$STATE_DIR/msa-result.XXXXXXXX.json")
    prep_status=0
    bio-msa prepare --model "$recipe" --fasta "$infile" --timeout "$seconds" \
      --bundle-result "$prep_result" || prep_status=$?
    if [ "$prep_status" -ne 0 ]; then rm -f "$prep_result"; exit "$prep_status"; fi
    msa_bundle=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["bundle"])' "$prep_result")
    rm -f "$prep_result"
  fi
  python3 "$TOOLS_SRC/msa/prepared.py" validate --bundle "$msa_bundle" --model "$recipe" --fasta "$infile"
fi
# Serialize jobs while environments and provider volume attachment are shared.
# The lock is independent of dc's accounting lock.
mkdir -p "$STATE_DIR" "$RESULTS_DIR"
lock_name=bio-submit
[ "$recipe" != msa ] || lock_name=msa-submit
exec 9>"$STATE_DIR/$lock_name.lock"
flock 9
# Queued submissions may have passed the first check before storage expired.
if [ -n "$db_nfs" ]; then "$storage_tool" check --volume "$db_volume"; fi
# Check installed data on the head before renting a worker. The worker still
# validates its own view: a readable head mount does not prove another client's
# database visibility or the completeness of the full MSA installation.
if [ "$recipe" = rfaa ] && [ -n "$db_nfs" ]; then
  python3 "$TOOLS_SRC/rfaa/databases.py" validate --root "${RFAA_DB_DIR:-/mnt/bio-databases/rfaa}" || {
    echo 'bio-submit: full RFAA databases are not ready on the head; finish installation and validation before launching' >&2
    exit 2
  }
elif [ "$recipe" = msa ] && [ "$sub" != install ] && [ "$sub" != convert ]; then
  msa_receipt="${MSA_DB_ROOT:-/mnt/bio-msa-databases/colabfold}/.msa-databases.json"
  [ -f "$msa_receipt" ] && [ -s "$msa_receipt" ] || {
    echo 'bio-submit: private MSA databases are not ready on the head; a nonempty final .msa-databases.json receipt is required' >&2
    exit 2
  }
fi
jobid="$recipe-$(date -u +%Y%m%d-%H%M%S)-$$"
LOCALOUT="$RESULTS_DIR/$jobid"; mkdir -p "$LOCALOUT"
exec > >(tee -a "$LOCALOUT/run.log") 2>&1
run="$SHARED_MNT/runs/$jobid"; mkdir -p "$run/in" "$run/out"
RIN=""; RLABELS=""; RPREP=""
if [ -n "$infile" ]; then cp "$infile" "$run/in/input.${infile##*.}"; RIN="$run/in/input.${infile##*.}"; fi
if [ -n "$panel_manifest_sha" ]; then
  python3 "$TOOLS_SRC/msa/panel.py" validate --manifest "$RIN" --expected-sha256 "$panel_manifest_sha"
  cp "$RIN" "$LOCALOUT/panel-manifest.json"
fi
if [ -n "$labels" ]; then cp "$labels" "$run/in/labels.csv"; RLABELS="$run/in/labels.csv"; fi
if [ -n "$msa_bundle" ]; then
  RPREP="$run/in/prepared"; mkdir -p "$RPREP"
  cp -a "$msa_bundle/." "$RPREP/"
fi
ROUT="$run/out"
# NFS clients have returned stale recipe contents after deployment. Snapshot
# authoritative code on the head and transmit it with the script over SSH.
bundle="$LOCALOUT/tools.tar.gz"
bundle_dirs=(recipes py requirements rfaa)
[ ! -d "$TOOLS_SRC/msa" ] || bundle_dirs+=(msa)
tar -czhf "$bundle" -C "$TOOLS_SRC" "${bundle_dirs[@]}"
bundle_sha256=$(sha256sum "$bundle" | cut -d ' ' -f1)
# printf %q preserves argument boundaries and prevents input text becoming code.
remote_file="$LOCALOUT/remote.sh"
{
  printf 'set -euo pipefail\n'
  printf 'export BIO_JOB_DEADLINE_EPOCH=$(( $(date +%%s) + 10#%s ))\n' "$seconds"
  printf 'export IN=%q OUT=%q LABELS=%q MODEL=%q SUB=%q CONTIGS=%q NUM=%q TEMP=%q NAME=%q\n' "$RIN" "$ROUT" "$RLABELS" "$model" "$sub" "$contigs" "$num" "$temp" "${name:-$recipe}"
  printf 'EXTRA_ARGS=('
  if [ "${#extra[@]}" -gt 0 ]; then printf ' %q' "${extra[@]}"; fi
  printf ' )\n'
  printf 'export EXTRA=%q\n' "${extra[*]:-}"
  printf 'SHARED_NFS=%q\n' "$SHARED_NFS"
  if [ "$recipe" = rfaa ]; then printf 'RFAA_DB_NFS=%q\n' "$db_nfs"; else printf 'RFAA_DB_NFS=""\n'; fi
  if [ "$recipe" = msa ]; then printf 'MSA_DB_NFS=%q\n' "$db_nfs"; else printf 'MSA_DB_NFS=""\n'; fi
  printf 'export MSA_DB_ROOT=%q BIO_MSA_BUNDLE=%q\n' "${MSA_DB_ROOT:-/mnt/bio-msa-databases/colabfold}" "$RPREP"
  printf 'export BIO_MSA_PANEL_SHA256=%q\n' "$panel_manifest_sha"
  printf 'export RFAA_DB_DIR=%q\n' "${RFAA_DB_DIR:-/mnt/bio-databases/rfaa}"
  printf 'export RFAA_CPU=%q RFAA_MEM_GB=%q\n' "${RFAA_CPU:-4}" "${RFAA_MEM_GB:-64}"
  # Protenix's ColabFold mode does not select the ColabFold host automatically.
  # Forward the configured endpoint to the new VM; its environment is separate.
  printf 'export MMSEQS_SERVICE_HOST_URL=%q\n' "${MMSEQS_SERVICE_HOST_URL:-https://api.colabfold.com}"
  cat <<'BUNDLE'
export BIO_TOOLS_DIR
BIO_TOOLS_DIR=$(mktemp -d /tmp/bio-tools.XXXXXXXX)
trap 'rm -rf -- "$BIO_TOOLS_DIR"' EXIT
# Keep Python bytecode off NFS: one inference stalled in a shared-cache OPEN
# even though fresh mounts could read the same file immediately.
export PYTHONPYCACHEPREFIX="$BIO_TOOLS_DIR/cache/python"
mkdir -p "$PYTHONPYCACHEPREFIX"
base64 --decode > "$BIO_TOOLS_DIR/bundle.tar.gz" <<'BIO_TOOLS_ARCHIVE'
BUNDLE
  base64 "$bundle"
  printf 'BIO_TOOLS_ARCHIVE\n'
  printf 'printf "%%s  %%s\\n" %q "$BIO_TOOLS_DIR/bundle.tar.gz" | sha256sum --check --status\n' "$bundle_sha256"
  cat <<'BUNDLE'
tar -xzf "$BIO_TOOLS_DIR/bundle.tar.gz" -C "$BIO_TOOLS_DIR" --no-same-owner
rm "$BIO_TOOLS_DIR/bundle.tar.gz"
BUNDLE
  printf 'printf "bio-submit: code bundle verified %%s\\n" %q\n' "$bundle_sha256"
  printf '# END VERIFIED TOOL BUNDLE\n'
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
  sudo mount -t nfs -o vers=4.1,nconnect=16,nolock "$SHARED_NFS" /mnt/bio-shared && break
  echo "[bio-submit] shared FS not ready, attempt $i/8"; sleep 10
done
mountpoint -q /mnt/bio-shared || { echo 'shared FS never mounted' >&2; exit 1; }
if [ -n "$RFAA_DB_NFS" ]; then
  sudo mkdir -p /mnt/bio-databases
  for i in 1 2 3 4 5 6 7 8; do
    mountpoint -q /mnt/bio-databases && break
    sudo mount -t nfs -o vers=4.1,nconnect=16,nolock,ro "$RFAA_DB_NFS" /mnt/bio-databases && break
    sleep 10
  done
  mountpoint -q /mnt/bio-databases || { echo 'RFAA database volume never mounted' >&2; exit 1; }
fi
if [ -n "$MSA_DB_NFS" ]; then
  sudo mkdir -p /mnt/bio-msa-databases
  msa_mount_options=vers=4.1,nconnect=16,nolock,ro
  case "$SUB" in install|convert) msa_mount_options=vers=4.1,nconnect=16,nolock ;; esac
  for i in 1 2 3 4 5 6 7 8; do
    mountpoint -q /mnt/bio-msa-databases && break
    sudo mount -t nfs -o "$msa_mount_options" "$MSA_DB_NFS" /mnt/bio-msa-databases && break
    sleep 10
  done
  mountpoint -q /mnt/bio-msa-databases || { echo 'MSA database volume never mounted' >&2; exit 1; }
fi
command -v uv >/dev/null 2>&1 || curl --fail -LsS https://astral.sh/uv/install.sh | sh
mkdir -p "$OUT"
source "$BIO_TOOLS_DIR/recipes/_common.sh"
REMOTE
  printf 'source "$BIO_TOOLS_DIR/recipes/%s.sh"\n' "$recipe"
  printf 'sync\n'
} > "$remote_file"
id=""; ip=""
cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  # A service stop can kill tee before this trap runs. Write cleanup directly
  # to the retained log so a broken output pipe cannot prevent worker removal.
  exec >> "$LOCALOUT/run.log" 2>&1
  if [ -n "$id" ]; then
    echo "bio-submit: removing $id"
    dc rm "$id" || {
      echo "bio-submit: ERROR deleting $id; inspect dc ls" >&2
      [ "$status" -ne 0 ] || status=1
    }
  fi
  if [ -f "$LOCALOUT/job.json" ]; then
    python3 - "$LOCALOUT/job.json" "$status" <<'PY' || {
import datetime, json, sys
path, status = sys.argv[1:]
with open(path) as stream:
    data = json.load(stream)
data.update(exit_status=int(status), finished=datetime.datetime.now(datetime.timezone.utc).isoformat())
with open(path, 'w') as stream:
    json.dump(data, stream, indent=2)
PY
      echo 'bio-submit: could not record final job status' >&2
      [ "$status" -ne 0 ] || status=1
    }
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
if [ -n "$db_nfs" ]; then
  "$storage_tool" track --volume "$db_volume" --job-dir "$LOCALOUT" --pid "$$"
fi
# Older CUDA wheels do not support Blackwell. CUDA 11 RF stacks use Ampere.
if [ -n "$gpu" ]; then candidates=("$gpu")
else
  case "$tier" in
    ampere_full) candidates=(1A100.22V 1A100.40S.22V) ;;
    ampere) candidates=(1A100.22V 1A6000.10V 1A100.40S.22V) ;;
    cuda128) candidates=(1A100.22V 1L40S.20V 1H100.80S.32V) ;;
    modern) candidates=(1A100.22V 1L40S.20V 1H100.80S.32V 1A6000.10V) ;;
    latest) candidates=(1A100.22V 1L40S.20V 1RTXPRO6000.30V 1H100.80S.32V) ;;
    msa) candidates=(CPU.360V.1440G) ;;
    msa_convert) candidates=(CPU.16V.64G) ;;
  esac
fi
for g in "${candidates[@]}"; do
  echo "bio-submit: launching $g ..."
  max_hours=$(python3 -c 'import sys; print((int(sys.argv[1])+900)/3600)' "$seconds")
  image_args=()
  [ "$g" != 1A6000.10V ] || image_args=(--image ubuntu-24.04-cuda-12.6-docker)
  if [ "$recipe" = msa ] && [[ "$g" = CPU.* ]]; then image_args=(--image ubuntu-24.04); fi
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
[ -n "$id" ] || { echo 'bio-submit: no compatible worker capacity' >&2; exit 5; }
if [ -n "$db_nfs" ]; then
  "$storage_tool" track --volume "$db_volume" --job-dir "$LOCALOUT" --pid "$$" --instance "$id"
fi
SSHO=(-i /root/.ssh/datacrunch_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3)
ready=0
for _ in $(seq 1 30); do
  if ssh "${SSHO[@]}" "root@$ip" true 2>/dev/null; then ready=1; break; fi
  sleep 8
done
[ "$ready" = 1 ] || { echo 'bio-submit: sshd never became ready' >&2; exit 1; }
python3 - "$LOCALOUT/job.json" "$jobid" "$recipe" "$id" "$ip" "$g" "$seconds" "$db_volume" "$bundle_sha256" <<'PY'
import json,sys,datetime
p,job,model,instance,ip,gpu,timeout,db_volume,bundle_sha256=sys.argv[1:]
with open(p,'w') as f: json.dump(dict(job=job,model=model,instance=instance,ip=ip,gpu=gpu,timeout=int(timeout),database_volume=db_volume or None,tools_sha256=bundle_sha256,started=datetime.datetime.now(datetime.timezone.utc).isoformat()),f,indent=2)
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
if [ "$recipe" = msa ] && [ "$sub" = prepare ]; then
  python3 "$TOOLS_SRC/msa/prepared.py" validate --bundle "$LOCALOUT/prepared" --model "$model" --fasta "$infile"
fi
if [ -n "$panel_manifest_sha" ]; then
  python3 "$TOOLS_SRC/msa/panel.py" verify --manifest "$LOCALOUT/panel-manifest.json" \
    --expected-sha256 "$panel_manifest_sha" --out "$LOCALOUT/panel"
fi
dc rm "$id"; id=""
if [ "$recipe" = msa ] && [ "$sub" = prepare ] && [ -n "$bundle_result" ]; then
    python3 - "$bundle_result" "$LOCALOUT/prepared" <<'PY'
import json, os, pathlib, sys, tempfile
path = pathlib.Path(sys.argv[1])
fd, temporary = tempfile.mkstemp(prefix='.msa-result-', dir=path.parent)
try:
    with os.fdopen(fd, 'w') as stream:
        json.dump({'bundle': sys.argv[2]}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
fi
echo "bio-submit: DONE — results at $LOCALOUT (head-local)"
