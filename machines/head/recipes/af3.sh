# AlphaFold 3 — installs the code + C++ data pipeline; inference requires the
# GATED model parameters (request from Google + EULA). Runs only if you've placed
# af3.bin at $SHARED/weights/alphafold3/. IN = AF3 input JSON.
SRC="$SHARED/src/alphafold3"; W="$SHARED/weights/alphafold3"; mkdir -p "$W"
[ -d "$SRC/.git" ] || git clone https://github.com/google-deepmind/alphafold3 "$SRC"
git -C "$SRC" checkout -q 85c4d20505fd5cef05eac22b534d4e793971ae69 || true
if [ ! -f "$W/af3.bin" ] && [ ! -f "$W/af3.bin.zst" ]; then
  echo "[af3] BLOCKED: model parameters are gated by Google (access form + EULA)."
  echo "[af3] Place approved af3.bin in $W/ then re-run. (Code is cloned at $SRC.)"
  exit 4
fi
VENV="$SHARED/envs/alphafold3"; sys_venv "$VENV"   # AF3 needs py>=3.12 = the node's system python3
( cd "$SRC" && UV_PROJECT_ENVIRONMENT="$VENV" uv sync && UV_PROJECT_ENVIRONMENT="$VENV" uv run build_data ) || echo "[af3] uv sync / build_data failed"
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
export XLA_PYTHON_CLIENT_PREALLOCATE=false TF_FORCE_UNIFIED_MEMORY=true
have_gpu
cd "$SRC"
# shellcheck disable=SC2086
"$VENV/bin/python" run_alphafold.py --json_path="$IN" --model_dir="$W" --output_dir="$OUT" --run_data_pipeline=false $EXTRA
