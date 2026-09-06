# RoseTTAFold-All-Atom. Full MSA/template preparation is the default; select
# SUB=single-seq explicitly for a database-free smoke test. IN = one-chain FASTA,
# or BIO_NATIVE_BUNDLE supplies a verified native assembly.
MODE="${SUB:-full}"
case "$MODE" in full|single-seq) ;; *) echo 'rfaa: --sub must be full or single-seq' >&2; exit 2 ;; esac
NATIVE_CONFIG=""; HAS_PROTEIN=1
if [ -n "${BIO_NATIVE_BUNDLE:-}" ]; then
  [ "${#EXTRA_ARGS[@]}" -eq 0 ] || { echo 'rfaa: extra native overrides are not supported for checked assemblies' >&2; exit 2; }
  NATIVE_CONFIG=$(python3 "$TOOLS/library/adapters.py" materialize \
    --bundle "$BIO_NATIVE_BUNDLE" --model rfaa --out "$OUT/native-input")
  HAS_PROTEIN=$(python3 "$TOOLS/library/rfaa_adapter.py" info --config "$NATIVE_CONFIG" --field has-protein)
  python3 "$TOOLS/library/rfaa_adapter.py" info --config "$NATIVE_CONFIG" --field proteins > "$OUT/native-proteins.tsv"
fi
if [ "$MODE" = full ] && [ "$HAS_PROTEIN" = 1 ]; then
  RFAA_MEM_GB="${RFAA_MEM_GB:-64}"
  [[ "$RFAA_MEM_GB" =~ ^[1-9][0-9]*$ ]] \
    || { echo 'rfaa: RFAA_MEM_GB must be a positive integer' >&2; exit 2; }
  available_kib=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  [[ "$available_kib" =~ ^[0-9]+$ ]] \
    || { echo 'rfaa: cannot determine available host memory' >&2; exit 2; }
  required_kib=$(( (RFAA_MEM_GB + 8) * 1024 * 1024 ))
  if (( available_kib < required_kib )); then
    echo "rfaa: full mode needs $((RFAA_MEM_GB + 8)) GiB available host RAM ($RFAA_MEM_GB GiB search limit + 8 GiB headroom); only $((available_kib / 1024 / 1024)) GiB is available. Choose a larger worker or lower RFAA_MEM_GB explicitly." >&2
    exit 2
  fi
fi
RFAA_SCRIPTS="$TOOLS/rfaa"
export RFAA_DB_DIR="${RFAA_DB_DIR:-/mnt/bio-databases/rfaa}"
if [ -z "$NATIVE_CONFIG" ]; then
  python3 "$RFAA_SCRIPTS/prepare.py" --fasta "$IN" --validate-only
fi
if [ "$MODE" = full ] && [ "$HAS_PROTEIN" = 1 ]; then
  python3 "$RFAA_SCRIPTS/databases.py" validate --root "$RFAA_DB_DIR"
fi
PY=$(nfs_py310)
VENV="$SHARED/envs/rfaa"; SRC="$SHARED/src/rfaa"; NAME="${NAME:-rfaa}"
"$VENV/bin/python" -c '' >/dev/null 2>&1 || { rm -rf "$VENV"; uv venv --python "$PY" "$VENV"; }
P="$VENV/bin/python"
[ -d "$SRC/.git" ] || git clone https://github.com/baker-laboratory/RoseTTAFold-All-Atom "$SRC"
git -C "$SRC" checkout -q d69ab3a73f8ede31a4cc005fbc076a341d848469
git -C "$SRC" submodule update --init --recursive -q
uv pip install --python "$P" -r "$TOOLS/requirements/rfaa.txt"
# PyTorch's cu118 wheel does not provide DGL's unversioned cu11 library names.
# The Ubuntu image has CUDA 12, so these libraries must travel with the venv.
uv pip install --python "$P" nvidia-cuda-runtime-cu11==11.8.89 \
  nvidia-cusparse-cu11==11.7.5.86 nvidia-curand-cu11==10.3.0.86
uv pip install --python "$P" 'dgl==1.1.2+cu118' --only-binary=dgl \
  -f https://data.dgl.ai/wheels/cu118/repo.html
uv pip install --python "$P" 'torch-scatter==2.1.2+pt20cu118' \
  'torch-sparse==0.6.18+pt20cu118' 'torch-cluster==1.6.3+pt20cu118' \
  --only-binary=torch-scatter,torch-sparse,torch-cluster \
  -f https://data.pyg.org/whl/torch-2.0.1+cu118.html
uv pip install --python "$P" torch-geometric==2.5.0
uv pip install --python "$P" "git+https://github.com/NVIDIA/dllogger.git@0540a43971f4a8a16693a9de9de73c1072020769"
uv pip install --python "$P" --no-deps "$SRC/rf2aa/SE3Transformer"
# Empty results from a completed HHsearch are valid; support that case as well
# as the explicitly selected single-seq mode. Never alter a real template DB.
"$P" "$TOOLS/py/rfaa_singleseq_patch.py" "$SRC/rf2aa/data/protein.py"
"$P" "$RFAA_SCRIPTS/patch_templates.py" "$SRC/rf2aa/data/parsers.py"
if [ -n "$NATIVE_CONFIG" ]; then
  "$P" "$TOOLS/library/rfaa_adapter.py" verify-source --config "$NATIVE_CONFIG" --source "$SRC" \
    > "$OUT/native-source-verification.json"
fi
WEIGHTS="$SRC/RFAA_paper_weights.pt"
if [ "$(stat -c %s "$WEIGHTS" 2>/dev/null || echo 0)" != 1336673865 ]; then
  dl "https://files.ipd.uw.edu/pub/RF-All-Atom/weights/RFAA_paper_weights.pt" "$WEIGHTS.part"
  [ "$(stat -c %s "$WEIGHTS.part")" = 1336673865 ] || { echo 'rfaa: incomplete model weights' >&2; exit 1; }
  mv "$WEIGHTS.part" "$WEIGHTS"
fi
if [ "$MODE" = full ] && [ "$HAS_PROTEIN" = 1 ]; then
  source "$RFAA_SCRIPTS/tools.sh"
  HHDB="$RFAA_DB_DIR/pdb100_2021Mar03/pdb100_2021Mar03"
else
  HHDB="$SHARED/cache/rfaa/blank-template/pdb100"
  mkdir -p "$(dirname "$HHDB")"
  : > "${HHDB}_pdb.ffindex"; printf '\0' > "${HHDB}_pdb.ffdata"
fi
prepare_chain() {
  local chain="$1" fasta="$2" prepared="$OUT/$NAME/$1"
  if [ "$MODE" = full ]; then
    "$P" "$RFAA_SCRIPTS/prepare.py" --fasta "$fasta" --out "$prepared" --root "$RFAA_DB_DIR" \
      --mode full --cpu "${RFAA_CPU:-4}" --mem "${RFAA_MEM_GB:-64}"
  else
    "$P" "$RFAA_SCRIPTS/prepare.py" --fasta "$fasta" --out "$prepared" --mode single-seq
  fi
}
if [ -n "$NATIVE_CONFIG" ]; then
  while IFS=$'\t' read -r chain fasta; do
    prepare_chain "$chain" "$fasta"
  done < "$OUT/native-proteins.tsv"
else
  prepare_chain A "$IN"
fi
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
export PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:512 WANDB_MODE=disabled
sp=$(echo "$VENV"/lib/python*/site-packages)
"$P" "$TOOLS/py/clear_execstack.py" "$sp"/torch/lib/*.so* "$sp"/dgl/*.so*
"$P" - <<'PY'
import torch, dgl
assert torch.cuda.is_available(), "RFAA needs a CUDA GPU"
graph = dgl.graph(([0], [1]), num_nodes=2).to("cuda")
graph.ndata["x"] = torch.ones((2, 1), device="cuda")
graph.update_all(dgl.function.copy_u("x", "m"), dgl.function.sum("m", "y"))
assert graph.ndata["y"].sum().item() == 1
print("RFAA CUDA graph check passed", torch.__version__, dgl.__version__)
PY
have_gpu
cd "$SRC"
if [ -n "$NATIVE_CONFIG" ]; then
  "$P" "$TOOLS/library/rfaa_adapter.py" run --config "$NATIVE_CONFIG" --source "$SRC" \
    --output "$OUT" --name "$NAME" --weights "$WEIGHTS" --hhdb "$HHDB" \
    --mode "$MODE" --receipt "$OUT/native-runtime.json"
else
  "$P" -m rf2aa.run_inference --config-name protein job_name="$NAME" output_path="$OUT" \
    checkpoint_path="$WEIGHTS" protein_inputs.A.fasta_file="$IN" \
    database_params.hhdb="$HHDB" database_params.sequencedb="$HHDB" "${EXTRA_ARGS[@]}"
fi
