# Protenix (ByteDance AF3 reproduction; Apache-2.0, commercial OK). Weights
# auto-download. No local DBs via --use_msa_server. IN = query FASTA.
# Needs torch 2.7.1+cu12 and deepspeed (JIT-compiles CUDA ops → node needs nvcc,
# present on the CUDA image). Weights come from a Beijing host (can be slow).
VENV="$SHARED/envs/protenix"
export PROTENIX_DATA_ROOT_DIR="$SHARED/protenix/release_data" TORCH_HOME="$SHARED/cache/torch" HF_HOME="$SHARED/cache/hf"
mkdir -p "$PROTENIX_DATA_ROOT_DIR"
sys_venv "$VENV"; P="$VENV/bin/python"
"$P" -c 'import torch' >/dev/null 2>&1 || uv pip install --python "$P" torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python "$P" protenix==2.0.0
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
SEQ=$(grep -v '^>' "$IN" | tr -d '\n\r \t'); JOB="${NAME:-protenix_job}"
J="$OUT/input.json"
cat > "$J" <<JSON
[ { "name": "$JOB", "sequences": [ { "proteinChain": { "sequence": "$SEQ", "count": 1 } } ] } ]
JSON
have_gpu
# shellcheck disable=SC2086
"$VENV/bin/protenix" predict --input "$J" --out_dir "$OUT" --seeds 101 --use_msa_server --use_template false $EXTRA
