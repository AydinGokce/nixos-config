# RFdiffusion — backbone generation (no MSA/DBs). torch 1.12+cu113 (needs py3.10,
# so use the shared-FS-hosted portable 3.10) + dgl 1.0 + CUDA-11 libs + SE3Transformer.
# Env vars from bio-submit: CONTIGS (hydra contig string), NUM (num_designs), IN (optional input pdb).
PY=$(nfs_py310)
VENV="$SHARED/envs/rfdiffusion"; SRC="$SHARED/src/rfdiffusion"; W="$SHARED/weights/rfdiffusion"
"$VENV/bin/python" -c '' >/dev/null 2>&1 || { rm -rf "$VENV"; uv venv --python "$PY" "$VENV"; }
P="$VENV/bin/python"
[ -d "$SRC/.git" ] || git clone https://github.com/RosettaCommons/RFdiffusion "$SRC"
git -C "$SRC" checkout -q 86507b6538f51fce57b5a72477165f03999ed7ae || true
uv pip install --python "$P" -r "$SHARED/tools/requirements/rfdiffusion.txt"
"$P" -c 'import dgl' >/dev/null 2>&1 || uv pip install --python "$P" dgl==1.0.2 -f https://data.dgl.ai/wheels/cu113/repo.html
uv pip install --python "$P" nvidia-cuda-runtime-cu11 nvidia-cusparse-cu11 nvidia-curand-cu11
uv pip install --python "$P" "git+https://github.com/NVIDIA/dllogger.git" || true
uv pip install --python "$P" --no-deps "$SRC/env/SE3Transformer"
uv pip install --python "$P" --no-deps -e "$SRC"
for kv in \
  6f5902ac237024bdd0c176cb93063dc4/Base_ckpt.pt \
  e29311f6f1bf1af907f9ef9f44b8328b/Complex_base_ckpt.pt \
  5532d2e1f3a4738decd58b19d633b3c3/ActiveSite_ckpt.pt ; do
  f=${kv##*/}; [ -s "$W/$f" ] || dl "http://files.ipd.uw.edu/pub/RFdiffusion/$kv" "$W/$f"
done
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
have_gpu
cd "$SRC"
set -- inference.output_prefix="$OUT/design" inference.model_directory_path="$W" \
       inference.num_designs="${NUM:-2}" "contigmap.contigs=${CONTIGS:-[100-100]}"
[ -n "${IN:-}" ] && set -- "$@" inference.input_pdb="$IN"
# shellcheck disable=SC2086
"$P" scripts/run_inference.py "$@" $EXTRA
