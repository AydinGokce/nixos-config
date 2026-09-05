# ESM-2 — embeddings / logits / scoring / mutation effects. No DBs; HF weights.
VENV="$SHARED/envs/esm2"
sys_venv "$VENV"
uv pip install --python "$VENV/bin/python" -r "$SHARED/tools/requirements/esm2.txt"
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
have_gpu
# shellcheck disable=SC2086
"$VENV/bin/python" "$SHARED/tools/py/esm_cli.py" "${SUB:-score}" -i "$IN" -o "$OUT/result" ${MODEL:+--model "$MODEL"} $EXTRA
