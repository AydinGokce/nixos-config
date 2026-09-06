# Boltz-2 — AF3-class folding (MIT; commercial OK). Weights auto-download (~7.6GB)
# to the shared cache. No local DBs: MSAs come from the remote ColabFold server
# (--use_msa_server). IN = query FASTA (single protein chain).
VENV="$SHARED/envs/boltz"; export BOLTZ_CACHE="$SHARED/cache/boltz"; mkdir -p "$BOLTZ_CACHE"
sys_venv "$VENV"
uv pip install --python "$VENV/bin/python" 'boltz[cuda]==2.2.1'
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
SEQ=$(grep -v '^>' "$IN" | tr -d '\n\r \t')
YAML="$OUT/input.yaml"
cat > "$YAML" <<YAML
version: 1
sequences:
  - protein:
      id: A
      sequence: $SEQ
YAML
have_gpu
# shellcheck disable=SC2086
"$VENV/bin/boltz" predict "$YAML" --use_msa_server --accelerator gpu --devices 1 \
  --out_dir "$OUT" --output_format pdb --cache "$BOLTZ_CACHE" "${EXTRA_ARGS[@]}"
