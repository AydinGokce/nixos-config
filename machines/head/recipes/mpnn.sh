# ProteinMPNN — fixed-backbone sequence design. Weights ship in the repo.
VENV="$SHARED/envs/proteinmpnn"; SRC="$SHARED/src/proteinmpnn"
sys_venv "$VENV"
uv pip install --python "$VENV/bin/python" -r "$SHARED/tools/requirements/proteinmpnn.txt"
[ -d "$SRC/.git" ] || git clone https://github.com/dauparas/ProteinMPNN "$SRC"
git -C "$SRC" checkout -q 8907e6671bfbfc92303b5f79c4b5e6ce47cdef57
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
have_gpu
"$VENV/bin/python" - <<'PY'
import torch
assert torch.cuda.is_available(), "ProteinMPNN needs a CUDA GPU on cloud workers"
assert torch.ones(1, device="cuda").sum().item() == 1
print("ProteinMPNN CUDA device:", torch.cuda.get_device_name(0))
PY
# shellcheck disable=SC2086
"$VENV/bin/python" "$SRC/protein_mpnn_run.py" --pdb_path "$IN" --out_folder "$OUT" \
  --num_seq_per_target "${NUM:-8}" --sampling_temp "${TEMP:-0.1}" --model_name "${MODEL:-v_48_020}" \
  --batch_size 1 --seed 37 "${EXTRA_ARGS[@]}"
# Upstream can exit zero without designs (for example CA + soluble weights, or
# all inputs filtered by max_length). Scoring-only modes instead produce NPZs.
[ -n "$(find "$OUT" -type f \( -path '*/seqs/*.fa' -o -path '*/score_only/*.npz' \
  -o -path '*/conditional_probs_only/*.npz' -o -path '*/unconditional_probs_only/*.npz' \) \
  -size +0c -print -quit)" ] || {
  echo 'mpnn: no sequence or scoring output produced; inspect the errors above' >&2
  exit 1
}
