# Run a model on a temporary GPU and fetch results before deleting the instance.
set -euo pipefail
umask 077
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
Models: boltz2, openfold3, protenix, rf3, rfaa, rfdiffusion, mpnn, esm, evolvepro, md
Inputs: --fasta FILE | --pdb FILE | --json FILE | --contigs '[50-50]'
Library: --construct REF | --assembly REF (pinned and checked before GPU rental)
Options: --labels CSV (EVOLVEpro), --sub CMD, --model NAME, --num N,
         --gpu TYPE, --spot, --timeout SECONDS (default 7200), -- EXTRA_ARGS
MSA: --msa-backend public|private, or --msa-bundle DIRECTORY for a prepared input.
RF3: --refresh-preparation captures a new search while preserving earlier cache entries.
Execution: --execution auto|resident|ephemeral (default auto; audited native defaults).
Boltz resident: explicitly request the audited seed with -- --seed 42.
Database jobs: bio-submit msa --sub install|convert|panel|prepare|serve|session [--model MODEL --fasta FILE]
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
  rf3) recipe=rf3; inkind=fasta; tier=cuda128 ;;
  rfaa) recipe=rfaa; inkind=fasta; tier=ampere ;;
  evolvepro) recipe=evolvepro; inkind=fasta; tier=modern ;;
  boltz2|boltz) recipe=boltz2; inkind=fasta; tier=latest ;;
  protenix) recipe=protenix; inkind=fasta; tier=cuda128 ;;
  openfold3|of3) recipe=openfold3; inkind=fasta; tier=latest ;;
  msa) recipe=msa; inkind=optional; tier=msa ;;
  md) recipe=md; inkind=bundle; tier=cuda128 ;;
  af3|alphafold3) recipe=af3; inkind=json; tier=latest ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
gpu=""; spot=""; infile=""; labels=""; model=""; sub=""; contigs=""; num=""; temp=""; name=""; seconds=7200; extra=()
msa_backend="${BIO_MSA_DEFAULT_BACKEND:-public}"; msa_bundle=""; bundle_result=""
library_ref=""; library_kind=""; library_bundle=""; library_sha=""; library_format=""; library_has_protein=""; input_flag=""
panel_manifest_sha=""
execution=auto; rf3_refresh_preparation=0; rf3_preparation_receipt=""
case "$recipe" in openfold3|boltz2|protenix|rf3) ;; *) msa_backend=public;; esac
if [[ "$recipe" = esm || "$recipe" = evolvepro ]]; then
  case "${1:-}" in ""|-*) ;; *) sub="$1"; shift ;; esac
fi
while [ $# -gt 0 ]; do
  case "$1" in
    --gpu|--worker|--model|--fasta|--pdb|--json|--in|--input-pdb|--labels|--sub|--contigs|--num-designs|--num|--num-seqs|--temp|--name|--timeout|--msa-backend|--msa-bundle|--bundle-result|--construct|--assembly|--execution)
      [ $# -ge 2 ] || { echo "bio-submit: $1 needs a value" >&2; exit 2; }
      case "$1" in
        --gpu|--worker) gpu="$2";; --model) model="$2";; --labels) labels="$2";;
        --msa-backend) msa_backend="$2";; --msa-bundle) msa_bundle="$2";; --bundle-result) bundle_result="$2";;
        --execution) execution="$2";;
        --construct|--assembly)
          [ -z "$library_ref" ] || { echo 'bio-submit: choose one library reference' >&2; exit 2; }
          library_ref="$2"; library_kind="${1#--}" ;;
        --sub) sub="$2";; --contigs) contigs="$2";; --temp) temp="$2";;
        --name) name="$2";; --timeout) seconds="$2";;
        --num-designs|--num|--num-seqs) num="$2";; *)
          [ -z "$infile" ] || { echo 'bio-submit: choose one input file' >&2; exit 2; }
          infile="$2"; input_flag="$1" ;;
      esac; shift ;;
    --spot) spot=--spot ;;
    --refresh-preparation) rf3_refresh_preparation=1 ;;
    --) shift; extra+=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) echo "bio-submit: unknown option $1 (put model-specific arguments after --)" >&2; exit 2 ;;
  esac
  shift
done
case "$execution" in auto|resident|ephemeral) ;; *) echo 'bio-submit: --execution must be auto, resident or ephemeral' >&2; exit 2;; esac
if [ "$rf3_refresh_preparation" = 1 ]; then
  [ "$recipe" = rf3 ] && [ -z "$msa_bundle" ] || { echo 'bio-submit: --refresh-preparation requires RF3 without an explicit --msa-bundle' >&2; exit 2; }
fi
[[ "$seconds" =~ ^[0-9]+$ ]] && (( seconds >= 60 && seconds <= 85500 )) || { echo 'bio-submit: timeout must be 60..85500 seconds' >&2; exit 2; }
[ -z "$infile" ] || [ -f "$infile" ] || { echo "bio-submit: input not found: $infile" >&2; exit 2; }
[ -z "$labels" ] || [ -f "$labels" ] || { echo "bio-submit: labels not found: $labels" >&2; exit 2; }
if [ "$recipe" = md ]; then
  [ -n "$infile" ] && [ -z "$labels$model$sub$contigs$num$temp$library_ref$msa_bundle" ] && [ "${#extra[@]}" -eq 0 ] || {
    echo 'bio-submit: MD accepts only a validated bundle, worker, timeout and name; use bio-md validate/submit' >&2; exit 2;
  }
  [ "$execution" != resident ] || { echo 'bio-submit: MD uses managed ephemeral workers' >&2; exit 2; }
  PYTHONPATH="$TOOLS_SRC" python3 -m md.bundle "$infile" >/dev/null
fi
[ -n "$infile$library_ref" ] || [[ "$inkind" = opt* ]] || { echo 'bio-submit: input required' >&2; exit 2; }
[ -z "$library_ref" ] || [ -z "$infile$contigs$msa_bundle" ] || { echo 'bio-submit: library references conflict with raw input, contigs or prepared bundles' >&2; exit 2; }
[ "$recipe" != rfdiffusion ] || [ -n "$contigs" ] || { echo 'bio-submit: --contigs required' >&2; exit 2; }
if [[ "$recipe" = protenix || "$recipe" = rf3 ]] && [ -n "$gpu" ]; then
  case "$gpu" in
    1A100.22V|1A100.40S.22V|1L40S.20V|1H100.80S.32V) ;;
    *) echo "bio-submit: ${recipe^} requires a validated A100, L40S or H100 worker with CUDA 12.8" >&2; exit 2 ;;
  esac
fi
case "$msa_backend" in public|private) ;; *) echo 'bio-submit: --msa-backend must be public or private' >&2; exit 2;; esac
head_preparation_acquire() {
  echo 'bio-submit: waiting for a head preparation slot (at most two preparations at once)'
  coproc BIO_PREPARATION_GATE { python3 "$TOOLS_SRC/py/head_preparation_gate.py" --state "$STATE_DIR" --timeout "$seconds"; }
  preparation_gate_pid=$BIO_PREPARATION_GATE_PID
  preparation_gate_input=${BIO_PREPARATION_GATE[1]}
  if ! IFS= read -r preparation_ready <&"${BIO_PREPARATION_GATE[0]}" || [ "$preparation_ready" != ready ]; then
    wait "$preparation_gate_pid" || true
    echo 'bio-submit: could not acquire a head preparation slot; no inference worker was launched' >&2
    exit 2
  fi
}
head_preparation_release() {
  printf 'release\n' >&"$preparation_gate_input"
  exec {preparation_gate_input}>&-
  wait "$preparation_gate_pid"
}
# Native CPU parsers/preparation run on the small head before GPU allocation.
# Bound those phases independently of the number of concurrent GPU workers.
# Database workers bypass this gate: private preparation can invoke one here.
if [ "$recipe" != msa ]; then
  head_preparation_acquire
fi
if [ "$recipe" = md ]; then
  PYTHONPATH="$TOOLS_SRC" python3 -m md.admission --bundle "$infile" --tools "$TOOLS_SRC" \
    --runtime "${BIO_MD_CPU_RUNTIME:-/var/lib/bio-md/runtime-cpu}" --state "${BIO_MD_ADMISSIONS:-/var/lib/bio-md/admissions}"
  md_variant=cuda
  [[ "$gpu" != CPU.* ]] || md_variant=cpu
  PYTHONPATH="$TOOLS_SRC" python3 -m md.launch --check --variant "$md_variant" \
    --runtime-root "${BIO_MD_RUNTIME_ARCHIVES:-/mnt/bio-shared/md-runtime}"
fi
if [ -n "$library_ref" ]; then
  case "$recipe" in boltz2|openfold3|protenix|rf3|rfaa|esm|evolvepro) ;; *) echo 'bio-submit: this model requires a structure/raw input, not a construct sequence' >&2; exit 2;; esac
  if [ "$msa_backend" = private ]; then
    case "$recipe" in boltz2|openfold3|protenix|rf3) ;; *) echo 'bio-submit: this model does not use the shared private MSA backend' >&2; exit 2;; esac
  fi
  # Resolve aliases once, before compiling; all later work uses this exact revision.
  library_ref=$(python3 - "$TOOLS_SRC/library" "${BIO_LIBRARY_ROOT:-/var/lib/bio-library}" "$library_ref" "$library_kind" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from registry import Registry, reference
record = Registry(sys.argv[2]).show(sys.argv[3])
if record['kind'] != sys.argv[4]:
    raise SystemExit('bio-submit: reference kind does not match --'+sys.argv[4])
print(reference(record))
PY
  )
  mkdir -p "$RESULTS_DIR/library-inputs"
  library_bundle="$RESULTS_DIR/library-inputs/$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
  compile_args=(--root "${BIO_LIBRARY_ROOT:-/var/lib/bio-library}" --ref "$library_ref" --model "$recipe" --out "$library_bundle" --msa-backend "$msa_backend")
  prefer_plain=0
  if [ "$execution" != ephemeral ] && [ "$msa_backend" = public ] && \
     [[ "$recipe" = protenix || "$recipe" = openfold3 || "$recipe" = boltz2 ]] && \
     [ -f "${BIO_INFERENCE_STATE:-/var/lib/bio-inference}/profiles/$recipe.json" ]; then
    prefer_plain=$(python3 - "$TOOLS_SRC/library" "${BIO_LIBRARY_ROOT:-/var/lib/bio-library}" "$library_ref" <<'PLAININPUT'
import pathlib, sys, tempfile
sys.path.insert(0, sys.argv[1])
from adapters import Registry, fasta, verify_snapshot
snapshot = Registry(sys.argv[2]).snapshot(sys.argv[3])
verify_snapshot(snapshot)
try:
    with tempfile.TemporaryDirectory() as folder:
        fasta(snapshot, pathlib.Path(folder))
except ValueError:
    print(0)
else:
    print(1)
PLAININPUT
    )
  fi
  if { [ "$msa_backend" = private ] && [ "$recipe" != rf3 ]; } || [ "$prefer_plain" = 1 ]; then
    compile_args+=(--plain-fasta)
  fi
  python3 "$TOOLS_SRC/library/runtime.py" "${compile_args[@]}"
  library_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "$library_bundle/bundle.json")
  library_format=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["format"])' "$library_bundle/bundle.json")
  library_has_protein=$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["has_protein"]))' "$library_bundle/bundle.json")
  infile="$library_bundle/$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["entrypoint"])' "$library_bundle/bundle.json")"
  if [ "$library_format" != protein-fasta ]; then
    for arg in "${extra[@]}"; do
      case "$arg" in
        --input*|--query*|--msa*|--use_msa*|--use-msa*|--template*|--use_template*|--use-template*|--runner_yaml*|--runner-yaml*|--out*|--dump*|--data*|--load*|--cache*|--ccd*|--protein*|--ligand*|--num_workers*|--num-workers*|--no_boltz2*|--model*|--checkpoint*|--config*|--use_template*|*.yaml|*.json)
          echo "bio-submit: native input conflicts with override $arg" >&2; exit 2 ;;
      esac
    done
  fi
elif [ -n "$infile" ] && [[ "$recipe" = boltz2 || "$recipe" = protenix || "$recipe" = openfold3 ]]; then
  [ "$input_flag" != --json ] || { echo 'bio-submit: raw JSON is not a FASTA; import typed constructs/assemblies with bio-library' >&2; exit 2; }
  python3 - "$infile" <<'PY'
import pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text()
lines = [s.strip() for s in text.splitlines() if s.strip()]
if not lines or not lines[0].startswith('>') or sum(s.startswith('>') for s in lines) != 1:
    raise SystemExit('bio-submit: use one protein FASTA record; use a library assembly for multiple chains')
seq = ''.join(lines[1:])
if not seq or set(seq) - set('ACDEFGHIKLMNPQRSTVWY'):
    raise SystemExit('bio-submit: FASTA requires canonical protein letters; use typed library inputs for DNA/RNA or modifications')
PY
fi
if [ "$recipe" = msa ]; then
  sub="${sub:-prepare}"
  case "$sub" in
    install|convert|serve|session)
      [ -z "$bundle_result" ] || { echo 'bio-submit: --bundle-result is only valid for MSA preparation' >&2; exit 2; } ;;
    prepare)
      [ -n "$infile" ] || { echo 'bio-submit: MSA preparation needs --fasta' >&2; exit 2; }
      case "$model" in openfold3|boltz2|protenix) ;;
        rf3) python3 "$TOOLS_SRC/rf3/msa.py" validate-queries --input "$infile" ;;
        *) echo 'bio-submit: MSA preparation needs --model openfold3|boltz2|protenix|rf3' >&2; exit 2;; esac ;;
    panel)
      [ -n "$infile" ] || { echo 'bio-submit: MSA panel needs --json MANIFEST.json' >&2; exit 2; } ;;
    *) echo 'bio-submit: MSA --sub must be install, convert, panel, prepare, serve or session' >&2; exit 2;;
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
  if [ -z "$library_bundle" ]; then python3 "$TOOLS_SRC/rfaa/prepare.py" --fasta "$infile" --validate-only; fi
  case "${sub:-full}" in
    full)
      # The default 64 GiB HHsuite limit needs the A100 nodes' 120 GB host RAM.
      # A6000 workers have only 60 GB; keep those for single-sequence jobs.
      tier=ampere_full
      if [ -n "$library_bundle" ] && [ "$library_has_protein" = 0 ]; then
        # No protein chains means no HHsuite searches or protein templates.
        # Preserve the native assembly while avoiding an unrelated DB gate.
        :
      else
      [ -n "${RFAA_DB_VOLUME:-}" ] && [ -n "${RFAA_DB_NFS:-}" ] \
        || { echo 'bio-submit: full RFAA needs the database volume configured in rfaa-storage.nix' >&2; exit 2; }
      bio-rfaa-storage check --volume "$RFAA_DB_VOLUME"
      db_nfs="$RFAA_DB_NFS"; db_volume="$RFAA_DB_VOLUME"; volumes+=(--volume "$db_volume")
      fi ;;
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
# RF3 accepts explicit per-chain A3Ms. Search on the head/public API or a
# separately accounted private MSA worker before renting an inference GPU.
if [ "$recipe" = rf3 ]; then
  [ -z "$sub$model$contigs$temp$labels" ] || { echo 'bio-submit: unsupported RF3 option' >&2; exit 2; }
  [ -z "$num" ] || extra+=("diffusion_batch_size=$num")
  for arg in "${extra[@]}"; do
    case "$arg" in
      n_recycles=*|num_steps=*|seed=*|diffusion_batch_size=*) ;;
      *) echo "bio-submit: unsupported RF3 override $arg; input/checkpoint/MSA settings are managed" >&2; exit 2 ;;
    esac
  done
  python3 - "$TOOLS_SRC/rf3/runtime.py" "${extra[@]}" <<'RF3SETTINGS'
import runpy, sys
runpy.run_path(sys.argv[1])['settings'](sys.argv[2:])
RF3SETTINGS
  [ "$input_flag" != --json ] || { echo 'bio-submit: import typed RF3 constructs/assemblies with bio-library; raw JSON assets cannot be staged implicitly' >&2; exit 2; }
  rf3_root="$RESULTS_DIR/rf3-inputs/$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
  mkdir -p "$rf3_root"
  if [ -n "$library_bundle" ]; then
    infile=$(python3 "$TOOLS_SRC/library/adapters.py" materialize --bundle "$library_bundle" \
      --model rf3 --out "$rf3_root/native" --expected-sha256 "$library_sha")
    rf3_input_args=(--native-json "$infile")
  else
    rf3_input_args=(--fasta "$infile")
  fi
  if [ -n "$msa_bundle" ]; then
    rf3_source_format=$(python3 - "$TOOLS_SRC/rf3" "$msa_bundle/input.json" <<'RF3SOURCEFORMAT'
import sys
sys.path.insert(0,sys.argv[1])
from prepare import validate
print(validate(sys.argv[2])['source_format'])
RF3SOURCEFORMAT
    )
    case "$rf3_source_format" in
      rf3-json) rf3_input_args=(--native-json "$infile") ;;
      fasta) rf3_input_args=(--fasta "$infile") ;;
      *) echo 'bio-submit: unsupported RF3 prepared source format' >&2; exit 2 ;;
    esac
  fi
  python3 "$TOOLS_SRC/rf3/msa.py" queries "${rf3_input_args[@]}" > "$rf3_root/queries.json"
  if [ -n "$msa_bundle" ]; then
    python3 - "$TOOLS_SRC/rf3" "$msa_bundle/msa-manifest.json" "$infile" <<'RF3CHECK'
import sys
sys.path.insert(0,sys.argv[1])
from prepare import read_json, file_hash
if read_json(sys.argv[2])['source_sha256'] != file_hash(sys.argv[3]):
    raise SystemExit('bio-submit: RF3 prepared input differs from supplied input file')
RF3CHECK
  else
    rf3_cache_args=(--rf3-prepare-only --model rf3 --shared "$SHARED_MNT" \
      --backend "$msa_backend" --endpoint "${MMSEQS_SERVICE_HOST_URL:-https://api.colabfold.com}" \
      --timeout "$seconds" --rf3-out "$rf3_root/prepared" --rf3-name "${name:-rf3_job}")
    [ -z "${MSA_DB_ROOT:-}" ] || rf3_cache_args+=(--rf3-database-root "$MSA_DB_ROOT")
    [ -z "$library_sha" ] || rf3_cache_args+=(--chemistry-sha "$library_sha")
    [ -z "$library_ref" ] || rf3_cache_args+=(--library-reference "$library_ref")
    [ "$rf3_refresh_preparation" = 0 ] || rf3_cache_args+=(--refresh-preparation)
    timeout --signal=TERM --kill-after=20 "$seconds" python3 "$TOOLS_SRC/inference/frontend.py" \
      "${rf3_input_args[@]}" "${rf3_cache_args[@]}" > "$rf3_root/preparation-result.json"
    msa_bundle="$rf3_root/prepared"
    rf3_preparation_receipt="$rf3_root/prepared.preparation-cache.json"
  fi
fi
# Prepare and validate complete native inputs before renting an inference GPU.
if [ "$recipe" != msa ] && [ "$recipe" != rf3 ] && { [ "$msa_backend" = private ] || [ -n "$msa_bundle" ]; }; then
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
# Unlock the head CPU permit before resident waiting or ephemeral GPU rental.
# Coprocess pipe descriptors are not inherited by model subprocesses.
if [ "$recipe" != msa ]; then
  head_preparation_release
fi
# Reuse an audited resident default configuration when the request matches it.
# All optional model arguments remain on the existing fully configurable route.
resident_eligible=0; resident_native_seed=""
case "$recipe" in
  protenix|openfold3)
    if [ -z "$gpu$spot$model$sub$contigs$num$temp$labels$name" ] && [ "${#extra[@]}" -eq 0 ] && \
       { [ -z "$library_bundle" ] || [ "$library_format" = protein-fasta ]; }; then resident_eligible=1; fi ;;
  boltz2)
    if [ -z "$gpu$spot$model$sub$contigs$num$temp$labels$name" ] && \
       { [ -z "$library_bundle" ] || [ "$library_format" = protein-fasta ]; }; then
      if [ "${#extra[@]}" -eq 0 ]; then
        resident_eligible=1
      elif { [ "${#extra[@]}" -eq 2 ] && [ "${extra[0]}" = --seed ] && [ "${extra[1]}" = 42 ]; } || \
           { [ "${#extra[@]}" -eq 1 ] && [ "${extra[0]}" = --seed=42 ]; }; then
        resident_eligible=1; resident_native_seed=42
      fi
    fi ;;
  rf3)
    if [ -z "$gpu$spot$model$sub$contigs$num$temp$labels" ] && [ "${#extra[@]}" -eq 0 ]; then resident_eligible=1; fi ;;
esac
if [ "$execution" != ephemeral ] && [ "$resident_eligible" = 1 ]; then
  resident_args=(--state "${BIO_INFERENCE_STATE:-/var/lib/bio-inference}" --model "$recipe" \
    --shared "$SHARED_MNT" --results "$RESULTS_DIR" --fasta "$infile" --backend "$msa_backend" \
    --timeout "$seconds" --endpoint "${MMSEQS_SERVICE_HOST_URL:-https://api.colabfold.com}")
  [ -z "$msa_bundle" ] || resident_args+=(--bundle "$msa_bundle")
  [ -z "$library_ref" ] || resident_args+=(--library-reference "$library_ref" --chemistry-sha "$library_sha")
  [ -z "$rf3_preparation_receipt" ] || resident_args+=(--rf3-preparation-receipt "$rf3_preparation_receipt")
  [ -z "$resident_native_seed" ] || resident_args+=(--native-seed "$resident_native_seed")
  resident_status=0
  python3 "$TOOLS_SRC/inference/frontend.py" "${resident_args[@]}" --probe || resident_status=$?
  if [ "$resident_status" = 0 ]; then
    exec python3 "$TOOLS_SRC/inference/frontend.py" "${resident_args[@]}"
  elif [ "$resident_status" != 78 ] || [ "$execution" = resident ]; then
    exit "$resident_status"
  fi
elif [ "$execution" = resident ]; then
  echo 'bio-submit: resident execution requires a compatible native-default input; use bio-inference for an explicit prepared configuration' >&2
  exit 2
fi
# New workers isolate every mutable runtime/cache tree on their own local disk.
# Shared leases let them run in parallel while excluding old deployments whose
# exclusive locks covered mutations of those same persistent NFS runtimes.
# Keep these leases until worker deletion, including error/timeout cleanup.
mkdir -p "$STATE_DIR" "$RESULTS_DIR"
exec 9>"$STATE_DIR/bio-submit.lock"
flock --shared 9
exec 8>"$STATE_DIR/msa-submit.lock"
flock --shared 8
if [ "$recipe" = msa ]; then
  # Database install/conversion and MSA server operations keep their existing
  # serialization, independently of concurrent molecular prediction workers.
  exec 7>"$STATE_DIR/msa-operation.lock"
  flock 7
fi
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
RIN=""; RLABELS=""; RPREP=""; RNATIVE=""; native_has_protein=""
if [ -n "$infile" ]; then cp "$infile" "$run/in/input.${infile##*.}"; RIN="$run/in/input.${infile##*.}"; fi
if [ -n "$panel_manifest_sha" ]; then
  python3 "$TOOLS_SRC/msa/panel.py" validate --manifest "$RIN" --expected-sha256 "$panel_manifest_sha"
  cp "$RIN" "$LOCALOUT/panel-manifest.json"
fi
if [ -n "$labels" ]; then cp "$labels" "$run/in/labels.csv"; RLABELS="$run/in/labels.csv"; fi
if [ -n "$rf3_preparation_receipt" ]; then cp "$rf3_preparation_receipt" "$LOCALOUT/rf3-preparation-cache.json"; fi
if [ -n "$msa_bundle" ]; then
  RPREP="$run/in/prepared"; mkdir -p "$RPREP"
  cp -a "$msa_bundle/." "$RPREP/"
fi
if [ -n "$library_bundle" ]; then
  python3 "$TOOLS_SRC/library/adapters.py" validate --bundle "$library_bundle" --model "$recipe" --expected-sha256 "$library_sha" >/dev/null
  cp -a "$library_bundle" "$LOCALOUT/library-input"
  if [ "$library_format" != protein-fasta ] && [ "$recipe" != rf3 ]; then
    RNATIVE="$run/in/native-bundle"
    cp -a "$library_bundle" "$RNATIVE"
    python3 "$TOOLS_SRC/library/adapters.py" validate --bundle "$RNATIVE" --model "$recipe" --expected-sha256 "$library_sha" >/dev/null
    native_has_protein=$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["has_protein"]))' "$RNATIVE/bundle.json")
  fi
fi
ROUT="$run/out"
public_msa_proxy=0
public_msa_head_port=${BIO_PUBLIC_MSA_HEAD_PORT:-18763}
if [[ "$recipe" = boltz2 || "$recipe" = protenix || "$recipe" = openfold3 ]] && \
   [ "$msa_backend" = public ] && [ -z "$msa_bundle" ] && [ "$library_has_protein" != 0 ]; then
  public_msa_proxy=1
  # Public queries leave through the head and hold one cross-worker query lock.
  # Fail before rental if the loopback forwarding destination is unavailable.
  python3 - "$public_msa_head_port" <<'PUBLICMSAREADY'
import http.client,json,sys
connection=None
try:
    port=int(sys.argv[1])
    if not 1 <= port <= 65535:
        raise ValueError('invalid port')
    # HTTPConnection ignores ambient proxies and does not follow redirects.
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
    connection.request('GET','/health')
    response=connection.getresponse()
    body=response.read(1025)
    if response.status != 200 or len(body)>1024:
        raise ValueError('invalid health status or size')
    value=json.loads(body)
    if value != {'service':'bio-public-msa-proxy','schema':1} or type(value['schema']) is not int:
        raise ValueError('unexpected health service or schema')
except (ValueError,OSError,http.client.HTTPException) as error:
    raise SystemExit(f'bio-submit: head public-MSA proxy unavailable: {error}; no worker launched')
finally:
    if connection is not None:
        connection.close()
PUBLICMSAREADY
fi
# Count only this model's runtime/checkpoint assets before rental. The quoted
# OS disk includes room for private copies and setup; dc accounts for its cost.
[ "$recipe" = msa ] || head_preparation_acquire
python3 "$TOOLS_SRC/py/worker_runtime.py" plan --shared "$SHARED_MNT" \
  --recipe "$recipe" --model "$model" --sub "$sub" > "$LOCALOUT/runtime-plan.json"
worker_os_size=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["os_size_gb"])' "$LOCALOUT/runtime-plan.json")
[ "$recipe" = msa ] || head_preparation_release
# NFS clients have returned stale recipe contents after deployment. Snapshot
# authoritative code on the head and transmit it with the script over SSH.
bundle="$LOCALOUT/tools.tar.gz"
bundle_dirs=(recipes py requirements rfaa)
[ ! -d "$TOOLS_SRC/msa" ] || bundle_dirs+=(msa)
[ ! -d "$TOOLS_SRC/library" ] || bundle_dirs+=(library)
[ ! -d "$TOOLS_SRC/rf3" ] || bundle_dirs+=(rf3)
[ "$recipe" != md ] || bundle_dirs+=(md)
tar -czhf "$bundle" -C "$TOOLS_SRC" "${bundle_dirs[@]}"
bundle_sha256=$(sha256sum "$bundle" | cut -d ' ' -f1)
# printf %q preserves argument boundaries and prevents input text becoming code.
remote_file="$LOCALOUT/remote.sh"
remote_msa_bundle="$RPREP"
[ "$recipe" != rf3 ] || remote_msa_bundle=""
{
  printf 'set -euo pipefail\n'
  printf 'export BIO_JOB_DEADLINE_EPOCH=$(( $(date +%%s) + 10#%s ))\n' "$seconds"
  if [ "$recipe" = md ]; then
    case "$gpu" in CPU.*) printf 'export BIO_MD_VARIANT=cpu\n' ;; *) printf 'export BIO_MD_VARIANT=cuda\n' ;; esac
  fi
  printf 'export IN=%q OUT=%q LABELS=%q MODEL=%q SUB=%q CONTIGS=%q NUM=%q TEMP=%q NAME=%q\n' "$RIN" "$ROUT" "$RLABELS" "$model" "$sub" "$contigs" "$num" "$temp" "${name:-$recipe}"
  printf 'EXTRA_ARGS=('
  if [ "${#extra[@]}" -gt 0 ]; then printf ' %q' "${extra[@]}"; fi
  printf ' )\n'
  printf 'export EXTRA=%q\n' "${extra[*]:-}"
  printf 'SHARED_NFS=%q\n' "$SHARED_NFS"
  if [ "$recipe" = rfaa ]; then printf 'RFAA_DB_NFS=%q\n' "$db_nfs"; else printf 'RFAA_DB_NFS=""\n'; fi
  if [ "$recipe" = msa ]; then printf 'MSA_DB_NFS=%q\n' "$db_nfs"; else printf 'MSA_DB_NFS=""\n'; fi
  printf 'export MSA_DB_ROOT=%q BIO_MSA_BUNDLE=%q\n' "${MSA_DB_ROOT:-/mnt/bio-msa-databases/colabfold}" "$remote_msa_bundle"
  if [ "$recipe" = rf3 ]; then printf 'export BIO_RF3_INPUT=%q\n' "$RPREP/input.json"; fi
  printf 'export BIO_MSA_PANEL_SHA256=%q\n' "$panel_manifest_sha"
  printf 'export BIO_MSA_SESSION_ID=%q BIO_MSA_SESSION_IDLE_SECONDS=%q BIO_MSA_SESSION_WARM=%q\n' \
    "${BIO_MSA_SESSION_ID:-}" "${BIO_MSA_SESSION_IDLE_SECONDS:-900}" "${BIO_MSA_SESSION_WARM:-report}"
  printf 'export BIO_NATIVE_BUNDLE=%q BIO_NATIVE_SHA256=%q BIO_NATIVE_HAS_PROTEIN=%q\n' "$RNATIVE" "$library_sha" "$native_has_protein"
  printf 'export RFAA_DB_DIR=%q\n' "${RFAA_DB_DIR:-/mnt/bio-databases/rfaa}"
  printf 'export RFAA_CPU=%q RFAA_MEM_GB=%q\n' "${RFAA_CPU:-4}" "${RFAA_MEM_GB:-64}"
  # Protenix's ColabFold mode does not select the ColabFold host automatically.
  # Forward the configured endpoint to the new VM; its environment is separate.
  printf 'export MMSEQS_SERVICE_HOST_URL=%q\n' "${MMSEQS_SERVICE_HOST_URL:-https://api.colabfold.com}"
  if [ "$public_msa_proxy" = 1 ]; then
    printf 'export BIO_PUBLIC_MSA_PROXY=http://127.0.0.1:18763 BIO_PUBLIC_MSA_LOCK=/mnt/bio-shared/coordination/public-msa.lock\n'
  fi
  cat <<'BUNDLE'
export BIO_TOOLS_DIR
BIO_TOOLS_DIR=$(mktemp -d /tmp/bio-tools.XXXXXXXX)
cleanup_worker_runtime() {
  local status=$?
  trap - EXIT
  if declare -F bio_worker_cleanup_runtime >/dev/null; then
    bio_worker_cleanup_runtime || { [ "$status" -ne 0 ] || status=1; }
  fi
  rm -rf -- "$BIO_TOOLS_DIR"
  exit "$status"
}
trap cleanup_worker_runtime EXIT
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
  printf 'base64 --decode > "$BIO_TOOLS_DIR/runtime-plan.json" <<\x27BIO_RUNTIME_PLAN\x27\n'
  base64 "$LOCALOUT/runtime-plan.json"
  printf 'BIO_RUNTIME_PLAN\n'
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
source "$BIO_TOOLS_DIR/recipes/_isolate-runtime.sh"
bio_worker_isolate_runtime /mnt/bio-shared "$BIO_TOOLS_DIR/runtime-plan.json"
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
  if [ "${recipe:-}" = md ] && [ -n "${ROUT:-}" ] && [ -d "$ROUT" ]; then
    # A killed SSH client cannot deliver a reliable remote signal. The native
    # worker also watches this per-job flag on persistent shared storage.
    [ -f "$ROUT/result.json" ] || touch "$ROUT/.cancel-requested" || true
    if [ -f "$ROUT/.worker.lock" ] && [ ! -f "$ROUT/result.json" ]; then
      echo 'bio-submit: allowing MD to seal its native checkpoint before cleanup'
      for _ in $(seq 1 45); do
        [ ! -f "$ROUT/result.json" ] || break
        sleep 2
      done
    fi
  fi
  if [ -n "$id" ]; then
    echo "bio-submit: removing $id"
    dc rm "$id" || {
      echo "bio-submit: ERROR deleting $id; inspect dc ls" >&2
      [ "$status" -ne 0 ] || status=1
    }
  fi
  if [ "${recipe:-}" = md ] && [ -n "${ROUT:-}" ] && [ -d "$ROUT" ]; then
    # Retrieve from the head's persistent mount even when cancellation killed
    # the ordinary SSH/rsync path. Unsealed crash checkpoints remain unusable.
    rsync -a "$ROUT/" "$LOCALOUT/" || {
      echo 'bio-submit: MD partial-result retention failed; inspect shared job outputs' >&2
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
msa_worker_image=""; launch_environment=()
if [ "$recipe" = msa ] && [ "$sub" != convert ] && [ -z "$gpu" ]; then
  selection_args=(select --tools-root "$TOOLS_SRC")
  [ -z "$spot" ] || selection_args+=(--spot-only)
  bio-msa-worker "${selection_args[@]}" > "$LOCALOUT/worker-choice.json"
  selection=$(python3 - "$LOCALOUT/worker-choice.json" <<'MSAWORKER'
import json, math, os, re, sys
value=json.load(open(sys.argv[1]))
kind=value['instance_type']; image=value['image']; spot=value['spot']
cap=float(os.environ.get('DC_MAX_INSTANCE_HOURLY') or 13)
valid=(value['schema']==1 and value['kind']=='msa-worker-choice' and value['reserved'] is False
       and value['location']=='FIN-02' and type(spot) is bool
       and isinstance(kind,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',kind)
       and image in ('ubuntu-24.04','ubuntu-24.04-cuda-12.8-open-docker')
       and math.isfinite(cap) and cap>0 and 0<float(value['price_per_hour'])<=13
       and float(value['conservative_gib'])>=768 and value['maximum_instance_hourly']==13)
if not valid:
    raise SystemExit('bio-submit: invalid MSA worker choice; no instance was launched')
print('\t'.join((kind,'yes' if spot else 'no',image,str(min(cap,13)))))
MSAWORKER
  )
  IFS=$'\t' read -r gpu selected_spot msa_worker_image msa_max_hourly <<< "$selection"
  spot=""; [ "$selected_spot" != yes ] || spot=--spot
  # The selector's availability/price is advisory. dc rechecks its own fresh
  # quote, budget and watchdog before POST, with this same upper bound.
  launch_environment=(env "DC_MAX_INSTANCE_HOURLY=$msa_max_hourly")
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
  if [[ "$recipe" = msa || "$recipe" = md ]] && [[ "$g" = CPU.* ]]; then image_args=(--image ubuntu-24.04); fi
  [ -z "$msa_worker_image" ] || image_args=(--image "$msa_worker_image")
  if out=$("${launch_environment[@]}" dc launch "$g" --loc "$LOC" ${spot:+"$spot"} "${volumes[@]}" "${image_args[@]}" --os-size "$worker_os_size" --max-hours "$max_hours" 2>&1); then
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
python3 - "$LOCALOUT/job.json" "$jobid" "$recipe" "$id" "$ip" "$g" "$seconds" "$db_volume" "$bundle_sha256" "$library_bundle" <<'PY'
import json,sys,datetime
p,job,model,instance,ip,gpu,timeout,db_volume,bundle_sha256,library=sys.argv[1:]
source = None
if library:
    with open(library+'/bundle.json') as f: native=json.load(f)
    source = {k: native[k] for k in ('source_ref', 'source_snapshot_sha256', 'sha256', 'format', 'msa_backend')}
with open(p,'w') as f: json.dump(dict(job=job,model=model,instance=instance,ip=ip,gpu=gpu,timeout=int(timeout),database_volume=db_volume or None,tools_sha256=bundle_sha256,library_input=source,started=datetime.datetime.now(datetime.timezone.utc).isoformat()),f,indent=2)
PY
python3 - "$LOCALOUT/job.json" "$LOCALOUT/runtime-plan.json" <<'RUNTIMEPLAN'
import hashlib,json,pathlib,sys
job,plan=map(pathlib.Path,sys.argv[1:]); value=json.loads(job.read_text()); runtime=json.loads(plan.read_text())
value['runtime_isolation']={'kind':runtime['isolation'],'plan':plan.name,
    'plan_sha256':hashlib.sha256(plan.read_bytes()).hexdigest(),'os_size_gb':runtime['os_size_gb']}
job.write_text(json.dumps(value,indent=2)+'\n')
RUNTIMEPLAN
if [ -n "$rf3_preparation_receipt" ]; then
  python3 - "$LOCALOUT/job.json" "$LOCALOUT/rf3-preparation-cache.json" <<'RF3CACHEJOB'
import hashlib,json,pathlib,sys
job,receipt=map(pathlib.Path,sys.argv[1:]);value=json.loads(job.read_text())
value['rf3_preparation']={'path':receipt.name,'sha256':hashlib.sha256(receipt.read_bytes()).hexdigest()}
job.write_text(json.dumps(value,indent=2)+'\n')
RF3CACHEJOB
fi
if [ "$recipe" = msa ] && [ "$sub" = session ]; then
  python3 "$TOOLS_SRC/msa/session_client.py" register-launch --state "$BIO_MSA_SESSION_STATE" \
    --job "$LOCALOUT/job.json" --remote-out "$ROUT"
fi
echo "bio-submit: running $recipe on $id ($ip), timeout ${seconds}s"
status=0
msa_forward=()
if [ "$public_msa_proxy" = 1 ]; then
  msa_forward=(-o ExitOnForwardFailure=yes -R "127.0.0.1:18763:127.0.0.1:$public_msa_head_port")
fi
timeout --signal=TERM --kill-after=60 "$seconds" ssh "${SSHO[@]}" "${msa_forward[@]}" "root@$ip" bash -s < "$remote_file" || status=$?
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
  if [ "$model" = rf3 ]; then
    python3 "$TOOLS_SRC/rf3/msa.py" validate-search --input "$LOCALOUT/prepared" --queries "$infile" >/dev/null
  else
    python3 "$TOOLS_SRC/msa/prepared.py" validate --bundle "$LOCALOUT/prepared" --model "$model" --fasta "$infile"
  fi
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
