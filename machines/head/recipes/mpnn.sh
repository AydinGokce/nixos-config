# ProteinMPNN — fixed-backbone sequence design. Weights ship in the repo.
VENV="$SHARED/envs/proteinmpnn"; SRC="$SHARED/src/proteinmpnn"
sys_venv "$VENV"
uv pip install --python "$VENV/bin/python" -r "$SHARED/tools/requirements/proteinmpnn.txt"
[ -f "$SRC/protein_mpnn_run.py" ] || git clone --depth 1 https://github.com/dauparas/ProteinMPNN "$SRC"
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
have_gpu
# shellcheck disable=SC2086
"$VENV/bin/python" "$SRC/protein_mpnn_run.py" --pdb_path "$IN" --out_folder "$OUT" \
  --num_seq_per_target "${NUM:-8}" --sampling_temp "${TEMP:-0.1}" --model_name v_48_020 \
  --batch_size 1 --seed 37 $EXTRA
