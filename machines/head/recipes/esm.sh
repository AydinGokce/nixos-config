# ESM-2 — embeddings / logits / scoring / mutation effects. No DBs; HF weights.
VENV="$SHARED/envs/esm2"
sys_venv "$VENV"
sub="${SUB:-score}"
input_args=(-i "$IN")
if [ "$sub" = mutate ]; then
  # The native mutate command accepts a WT sequence, not an input FASTA. Read
  # exactly one record before loading model weights and preserve its sequence.
  seq=$("$VENV/bin/python" - "$TOOLS/py" "$IN" <<'PY'
import argparse
import sys
sys.path.insert(0, sys.argv[1])
from esm_cli import read_seqs
records = read_seqs(argparse.Namespace(input=sys.argv[2], seq=None))
if len(records) != 1 or not records[0][1]:
    sys.exit("esm mutate: input FASTA must contain exactly one nonempty WT sequence")
print(records[0][1])
PY
  )
  input_args=(-s "$seq")
fi
uv pip install --python "$VENV/bin/python" -r "$TOOLS/requirements/esm2.txt"
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
have_gpu
# shellcheck disable=SC2086
args=(); [ -z "${MODEL:-}" ] || args+=(--model "$MODEL")
"$VENV/bin/python" "$TOOLS/py/esm_cli.py" "$sub" "${input_args[@]}" \
  -o "$OUT/result" "${args[@]}" "${EXTRA_ARGS[@]}" --device cuda
