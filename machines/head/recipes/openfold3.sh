# OpenFold3 (AF3-class open reimpl; Apache-2.0 code+weights, commercial OK). Weights
# fetched by setup_openfold from public S3 (not gated). No local DBs via
# --use_msa_server=True (remote ColabFold API; rate-limited). IN = query FASTA.
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
SEQ=$(grep -v '^>' "$IN" | tr -d '\n\r \t'); JOB="${NAME:-of3_job}"
J="$OUT/query.json"
cat > "$J" <<JSON
{ "queries": { "$JOB": { "chains": [ { "molecule_type": "protein", "chain_ids": ["A"], "sequence": "$SEQ" } ] } } }
JSON
have_gpu
# shellcheck disable=SC2086
"$VENV/bin/run_openfold" predict --query_json="$J" --use_msa_server=True --output_dir="$OUT" "${EXTRA_ARGS[@]}"
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
