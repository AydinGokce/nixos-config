# OpenFold3 (AF3-class open reimpl; Apache-2.0 code+weights, commercial OK). Weights
# fetched by setup_openfold from public S3 (not gated). No local DBs via
# --use_msa_server=True (remote ColabFold API; rate-limited). IN = query FASTA.
VENV="$SHARED/envs/openfold3"; export OPENFOLD_CACHE="$SHARED/openfold3/cache"; mkdir -p "$OPENFOLD_CACHE"
sys_venv "$VENV"; P="$VENV/bin/python"
"$P" -c 'import torch' >/dev/null 2>&1 || uv pip install --python "$P" torch --index-url https://download.pytorch.org/whl/cu124
uv pip install --python "$P" openfold3
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
# one-time weights fetch (cached on the shared FS via OPENFOLD_CACHE)
"$VENV/bin/setup_openfold" --non-interactive || true
SEQ=$(grep -v '^>' "$IN" | tr -d '\n\r \t'); JOB="${NAME:-of3_job}"
J="$OUT/query.json"
cat > "$J" <<JSON
{ "queries": { "$JOB": { "chains": [ { "molecule_type": "protein", "chain_ids": ["A"], "sequence": "$SEQ" } ] } } }
JSON
have_gpu
# shellcheck disable=SC2086
"$VENV/bin/run_openfold" predict --query_json="$J" --use_msa_server=True --output_dir="$OUT" $EXTRA
