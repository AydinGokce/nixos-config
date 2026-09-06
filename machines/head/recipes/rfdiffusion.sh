# RFdiffusion — backbone generation (no MSA/DBs). torch 1.12+cu113 (needs py3.10,
# so use the shared-FS-hosted portable 3.10) + dgl 1.0 + CUDA-11 libs + SE3Transformer.
# Env vars from bio-submit: CONTIGS (hydra contig string), NUM (num_designs), IN (optional input pdb).
PY=$(nfs_py310)
VENV="$SHARED/envs/rfdiffusion"; SRC="$SHARED/src/rfdiffusion"; W="$SHARED/weights/rfdiffusion"
"$VENV/bin/python" -c '' >/dev/null 2>&1 || { rm -rf "$VENV"; uv venv --python "$PY" "$VENV"; }
P="$VENV/bin/python"
[ -d "$SRC/.git" ] || git clone https://github.com/RosettaCommons/RFdiffusion "$SRC"
git -C "$SRC" checkout -q 86507b6538f51fce57b5a72477165f03999ed7ae
uv pip install --python "$P" -r "$SHARED/tools/requirements/rfdiffusion.txt"
uv pip install --python "$P" 'dgl==1.0.2+cu113' --only-binary=dgl \
  -f https://data.dgl.ai/wheels/cu113/repo.html
# Pin the runtime set tested with this old torch/DGL pair. DGL cannot use the
# worker image's CUDA 12 libraries in place of these CUDA 11 sonames.
uv pip install --python "$P" nvidia-cuda-runtime-cu11==11.8.89 \
  nvidia-cusparse-cu11==11.7.5.86 nvidia-curand-cu11==10.3.0.86
uv pip install --python "$P" "git+https://github.com/NVIDIA/dllogger.git@0478734ff7be75adde8d160e04872664d1c62e5f"
uv pip install --python "$P" --no-deps "$SRC/env/SE3Transformer"
uv pip install --python "$P" --no-deps -e "$SRC"
for kv in \
  6f5902ac237024bdd0c176cb93063dc4/Base_ckpt.pt \
  e29311f6f1bf1af907f9ef9f44b8328b/Complex_base_ckpt.pt \
  5532d2e1f3a4738decd58b19d633b3c3/ActiveSite_ckpt.pt ; do
  f=${kv##*/}; [ -s "$W/$f" ] || dl "http://files.ipd.uw.edu/pub/RFdiffusion/$kv" "$W/$f"
done
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
sp=$(echo "$VENV"/lib/python*/site-packages)
"$P" "$SHARED/tools/py/clear_execstack.py" "$sp"/torch/lib/*.so* "$sp"/dgl/*.so*
"$P" - <<'PY'
import torch, dgl
from se3_transformer.model import SE3Transformer
import rfdiffusion.inference.model_runners
assert torch.cuda.is_available(), "RFdiffusion needs a CUDA GPU"
graph = dgl.graph(([0], [1]), num_nodes=2).to("cuda")
graph.ndata["x"] = torch.ones((2, 1), device="cuda")
graph.update_all(dgl.function.copy_u("x", "m"), dgl.function.sum("m", "y"))
assert graph.ndata["y"].sum().item() == 1
print("RFdiffusion CUDA/SE3 check passed", torch.__version__, dgl.__version__)
PY
have_gpu
cd "$SRC"
set -- inference.output_prefix="$OUT/design" inference.model_directory_path="$W" \
       inference.num_designs="${NUM:-2}" "contigmap.contigs=${CONTIGS:-[100-100]}"
[ -n "${IN:-}" ] && set -- "$@" inference.input_pdb="$IN"
# shellcheck disable=SC2086
"$P" scripts/run_inference.py "$@" "${EXTRA_ARGS[@]}"
