# OpenFold3 (AF3-class open reimpl; Apache-2.0 code+weights, commercial OK). Weights
# fetched by setup_openfold from public S3 (not gated). No local DBs via
# --use_msa_server=True (remote ColabFold API; rate-limited). IN = query FASTA.
if [ -n "${BIO_NATIVE_BUNDLE:-}" ]; then
  [ -z "${BIO_MSA_BUNDLE:-}" ] || { echo 'openfold3: conflicting native/prepared inputs' >&2; exit 2; }
  for arg in "${EXTRA_ARGS[@]}"; do
    case "$arg" in
      --query*|--use_msa*|--use-msa*|--use_template*|--use-template*|--runner_yaml*|--runner-yaml*|--out*)
        echo "openfold3: native input conflicts with $arg" >&2; exit 2 ;;
    esac
  done
fi
if [ -n "${BIO_MSA_BUNDLE:-}" ]; then
  python3 "$TOOLS/msa/prepared.py" validate --bundle "$BIO_MSA_BUNDLE" --model openfold3 --fasta "$IN"
  for arg in "${EXTRA_ARGS[@]}"; do
    case "$arg" in
      --query*|--use_msa*|--use-msa*|--use_template*|--use-template*|--runner_yaml*|--runner-yaml*)
        echo "openfold3: prepared input conflicts with $arg" >&2; exit 2 ;;
    esac
  done
fi
VENV="$SHARED/envs/openfold3"
# setup_openfold writes weights under $HOME/.openfold3 while run_openfold reads
# $OPENFOLD_CACHE — so point BOTH at one dir on the share, else run_openfold
# never finds the checkpoint (and the weights would die with the node anyway).
OF3_HOME="$SHARED/openfold3/home"; export OPENFOLD_CACHE="$OF3_HOME/.openfold3"
mkdir -p "$OPENFOLD_CACHE"
sys_venv "$VENV"; P="$VENV/bin/python"
uv pip install --python "$P" torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python "$P" openfold3==0.5.0 torch==2.7.1
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
# Setup checks the release's exact checkpoint and CCD cache. A failed download
# must abort instead of surfacing later as an opaque missing-checkpoint error.
CFG="$OPENFOLD_CACHE/setup_config.json"
printf '{"openfold_cache":"%s","param_directory":"%s"}\n' "$OPENFOLD_CACHE" "$OPENFOLD_CACHE" > "$CFG"
HOME="$OF3_HOME" "$VENV/bin/setup_openfold" --config "$CFG"
JOB="${NAME:-of3_job}"
J="$OUT/query.json"
runner_cfg="$OUT/preparation-runner.yaml"
msa_args=(--use_msa_server=True)
if [ -n "${BIO_NATIVE_BUNDLE:-}" ]; then
  J=$("$P" "$TOOLS/library/adapters.py" materialize --bundle "$BIO_NATIVE_BUNDLE" \
      --model openfold3 --out "$OUT/native-input")
  cd "$OUT/native-input"
  [ "$BIO_NATIVE_HAS_PROTEIN" = 1 ] || msa_args=(--use_msa_server=False)
  "$P" "$TOOLS/msa/prepared.py" openfold-runner-config --out "$OUT" --write "$runner_cfg"
elif [ -n "${BIO_MSA_BUNDLE:-}" ]; then
  J=$("$P" "$TOOLS/msa/prepared.py" materialize --bundle "$BIO_MSA_BUNDLE" \
      --model openfold3 --fasta "$IN" --out "$OUT/prepared-native")
  "$P" "$TOOLS/msa/prepared.py" openfold-runner-config --out "$OUT" \
    --prepared-runtime "$OUT/prepared-native/runtime.json" --write "$runner_cfg"
  # In pinned 0.5.0 these flags gate preparation only. Native template paths
  # and directories still feed the dataset's template features. Reprocessing
  # the final template-cache NPZ as a raw hit alignment would lose templates.
  msa_args=(--use_msa_server=False --use_templates=False)
else
SEQ=$(grep -v '^>' "$IN" | tr -d '\n\r \t')
cp "$IN" "$OUT/reference_input.fasta"
cat > "$J" <<JSON
{ "queries": { "$JOB": { "chains": [ { "molecule_type": "protein", "chain_ids": ["A"], "sequence": "$SEQ" } ] } } }
JSON
  # Preserve custom scientific runner settings while retaining template files
  # that upstream normally deletes before the recipe can capture them.
  base_args=(); forward_args=()
  for ((i=0; i<${#EXTRA_ARGS[@]}; i++)); do
    case "${EXTRA_ARGS[i]}" in
      --runner_yaml|--runner-yaml)
        ((i+1<${#EXTRA_ARGS[@]})) || { echo 'openfold3: missing runner YAML path' >&2; exit 2; }
        base_args=(--base "${EXTRA_ARGS[i+1]}"); i=$((i+1)) ;;
      --runner_yaml=*|--runner-yaml=*) base_args=(--base "${EXTRA_ARGS[i]#*=}") ;;
      *) forward_args+=("${EXTRA_ARGS[i]}") ;;
    esac
  done
  EXTRA_ARGS=("${forward_args[@]}")
  "$P" "$TOOLS/msa/prepared.py" openfold-runner-config --out "$OUT" \
    "${base_args[@]}" --write "$runner_cfg"
fi
have_gpu
# shellcheck disable=SC2086
openfold_command=("$VENV/bin/run_openfold")
if [ -n "${BIO_PUBLIC_MSA_PROXY:-}" ]; then
  openfold_command=("$P" "$TOOLS/py/public_msa_client.py" --model openfold3 --entrypoint "$VENV/bin/run_openfold" --)
fi
"${openfold_command[@]}" predict --query_json="$J" "${msa_args[@]}" \
  --runner_yaml="$runner_cfg" --output_dir="$OUT" "${EXTRA_ARGS[@]}"
# The upstream runner catches per-query inference/output errors. Its process
# can exit zero with failed queries, so check the summary and real structures.
"$P" - "$OUT" <<'PY'
from pathlib import Path
import re
import sys
out = Path(sys.argv[1])
summary = (out / "summary.txt").read_text()
success = re.search(r"Successful Queries:\s*(\d+)", summary)
failed = re.search(r"Failed Queries:\s*(\d+)", summary)
if (not success or int(success[1]) < 1 or not failed or int(failed[1]) != 0
        or not any(path.stat().st_size for path in out.rglob("*_model.cif"))):
    sys.exit("openfold3: incomplete prediction; inspect summary.txt and inference errors")
PY
if [ -z "${BIO_MSA_BUNDLE:-}" ] && [ -z "${BIO_NATIVE_BUNDLE:-}" ]; then
  "$P" "$TOOLS/msa/prepared.py" capture --model openfold3 --run-dir "$OUT" \
    --out "$OUT/prepared-bundle" --source public --endpoint https://api.colabfold.com \
    --trust-native-npz
fi
