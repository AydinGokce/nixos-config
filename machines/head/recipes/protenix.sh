# Protenix (ByteDance AF3 reproduction; Apache-2.0, commercial OK). Weights
# auto-download. Uses the remote ColabFold MSA server. IN = query FASTA, or
# BIO_NATIVE_BUNDLE supplies a validated model-native molecular assembly.
# Uses torch 2.7.1+cu128 and the default cuEquivariance kernels. Importing its
# fused LayerNorm also requires nvcc and Ninja. Weights download can be slow.
if [ -n "${BIO_NATIVE_BUNDLE:-}" ]; then
  [ -z "${BIO_MSA_BUNDLE:-}" ] || {
    echo 'protenix: native molecular and prepared single-protein bundles conflict' >&2; exit 2
  }
  for arg in "${EXTRA_ARGS[@]}"; do
    case "$arg" in
      -i*|-o*|--input*|--out_dir*|--dump_dir*|--use_default_params*)
        echo "protenix: native input conflicts with $arg" >&2; exit 2 ;;
    esac
  done
elif [ -n "${BIO_MSA_BUNDLE:-}" ]; then
  python3 "$TOOLS/msa/prepared.py" validate --bundle "$BIO_MSA_BUNDLE" --model protenix --fasta "$IN"
  for arg in "${EXTRA_ARGS[@]}"; do
    case "$arg" in
      -i|--input*|--use_msa*|--msa_*|--use_template*|--use_rna_msa*|--use_default_params*)
        echo "protenix: prepared input conflicts with $arg" >&2; exit 2 ;;
    esac
  done
fi
VENV="$SHARED/envs/protenix"
# protenix 2.0.0 reads PROTENIX_ROOT_DIR (NOT ..._DATA_...) and keeps checkpoints
# in $PROTENIX_ROOT_DIR/checkpoint; unset it and weights re-download from the slow
# Beijing host onto the ephemeral node's disk on EVERY run.
export PROTENIX_ROOT_DIR="$SHARED/protenix/release_data"
# Selecting ColabFold mode alone still defaults upstream to the Protenix API.
# Set the matching endpoint before importing the module that reads this env var.
export MMSEQS_SERVICE_HOST_URL="${MMSEQS_SERVICE_HOST_URL:-https://api.colabfold.com}"
mkdir -p "$PROTENIX_ROOT_DIR" "$TORCH_HOME" "$HF_HOME"
sys_venv "$VENV"; P="$VENV/bin/python"
# PyTorch invokes Ninja by name when Protenix JIT-compiles its fused LayerNorm.
# Calling the venv's Python directly does not otherwise expose venv executables.
export PATH="$VENV/bin:/usr/local/cuda/bin:$PATH"
"$P" -c 'import torch; assert torch.__version__ == "2.7.1+cu128"' >/dev/null 2>&1 || uv pip install --python "$P" torch==2.7.1+cu128 torchvision==0.22.1+cu128 torchaudio==2.7.1+cu128 --index-url https://download.pytorch.org/whl/cu128
# scikit-learn-extra 0.3.0 (hard-pinned by protenix, no cp312 wheel) must build
# from source, and its isolated build would grab numpy 1.26 → the extension then
# dies at import under protenix's numpy 2.x ("compiled using NumPy 1.x"). So
# preinstall the build deps and build it WITHOUT isolation against numpy 2.x.
# cython<3.1: its .pyx uses `from numpy.math cimport INFINITY`, dropped in 3.1.
export DS_BUILD_OPS=0
BC=/tmp/protenix-build-constraints.txt
printf 'numpy==2.4.1\ncython<3.1\n' > "$BC"; export UV_BUILD_CONSTRAINT="$BC"
# Pin NumPy before probing the extension, including when reusing a NumPy-1 env.
uv pip install --python "$P" 'setuptools>=64' wheel 'cython<3.1' numpy==2.4.1
"$P" -c 'import sklearn_extra.cluster' >/dev/null 2>&1 || {
  # A normal install skips an already installed but ABI-broken copy. Rebuild
  # this package from source so old cached wheels cannot keep the broken ABI.
  uv pip install --python "$P" --no-cache --no-build-isolation --no-binary scikit-learn-extra \
    --reinstall-package scikit-learn-extra scikit-learn-extra==0.3.0
}
uv pip install --python "$P" protenix==2.0.0
# fail fast here rather than mid-predict if the numpy ABI still mismatches
"$P" -c 'import sklearn_extra.cluster'
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
"$P" - <<'PY'
from pathlib import Path
import re
import subprocess
import torch
from torch.utils.cpp_extension import CUDA_HOME, verify_ninja_availability

verify_ninja_availability()
assert CUDA_HOME, "protenix: CUDA toolkit/nvcc was not found"
nvcc = Path(CUDA_HOME) / "bin/nvcc"
assert nvcc.is_file(), f"protenix: missing {nvcc}"
version = subprocess.check_output([str(nvcc), "--version"], text=True)
release = re.search(r"release ([0-9]+\.[0-9]+)", version)
assert release and release.group(1) == torch.version.cuda, (
    f"protenix: nvcc must match torch CUDA {torch.version.cuda}: {version}"
)
assert torch.cuda.is_available(), "protenix: a CUDA GPU is required"
major, minor = torch.cuda.get_device_capability()
assert (major, minor) in {(8, 0), (8, 9), (9, 0)}, (
    "protenix: pinned Torch/Triton/cuEquivariance stack requires A100, L40S or H100; "
    f"GPU architecture {major}.{minor} is unsupported"
)
arches = subprocess.check_output([str(nvcc), "--list-gpu-arch"], text=True)
assert f"compute_{major}{minor}" in arches.split(), (
    f"protenix: nvcc does not support GPU architecture {major}.{minor}"
)
print("Protenix CUDA toolchain:", nvcc, "GPU:", torch.cuda.get_device_name(0), flush=True)
# Compile/import now and execute the real kernel, so missing runtime support
# fails before downloading checkpoints or submitting an MSA request.
from protenix.model.layer_norm import FusedLayerNorm
layer = FusedLayerNorm(32).cuda()
x = torch.randn(2, 32, device="cuda")
with torch.no_grad():
    torch.testing.assert_close(layer(x), torch.nn.functional.layer_norm(x, (32,)),
                               rtol=1e-4, atol=1e-4)
print("Protenix fused CUDA LayerNorm check passed", flush=True)
PY
J="$OUT/input.json"
if [ -n "${BIO_NATIVE_BUNDLE:-}" ]; then
  J=$(python3 "$TOOLS/library/adapters.py" materialize --bundle "$BIO_NATIVE_BUNDLE" \
      --model protenix --out "$OUT/native-input")
  # Native FILE_ ligand paths remain relative to this verified bundle.
  cd "$OUT/native-input"
elif [ -n "${BIO_MSA_BUNDLE:-}" ]; then
  J=$("$P" "$TOOLS/msa/prepared.py" materialize --bundle "$BIO_MSA_BUNDLE" \
      --model protenix --fasta "$IN" --out "$OUT/prepared-native")
  # Prepared inference must never fall back to a public search. Keep MSA
  # featurization enabled and verify the native search predicate before CLI.
  export MMSEQS_SERVICE_HOST_URL=http://127.0.0.1:9
  "$P" - "$J" <<'PY'
import json
import sys
from runner.msa_search import need_msa_search
with open(sys.argv[1]) as handle:
    queries = json.load(handle)
assert queries and not any(need_msa_search(query) for query in queries), (
    "protenix: prepared input unexpectedly requires an MSA search"
)
print("Protenix prepared MSA paths verified; remote generation disabled", flush=True)
PY
else
SEQ=$(grep -v '^>' "$IN" | tr -d '\n\r \t'); JOB="${NAME:-protenix_job}"
cp "$IN" "$OUT/reference_input.fasta"
cat > "$J" <<JSON
[ { "name": "$JOB", "sequences": [ { "proteinChain": { "sequence": "$SEQ", "count": 1 } } ] } ]
JSON
fi
have_gpu
# shellcheck disable=SC2086
# The function is named predict upstream, but 2.0.0 registers it as `pred`.
# Keep use_msa true for prepared input: false also disables MSA featurization.
# Existing validated paired/unpaired paths make the native search unnecessary.
protenix_command=("$VENV/bin/protenix")
if [ -n "${BIO_PUBLIC_MSA_PROXY:-}" ]; then
  protenix_command=("$P" "$TOOLS/py/public_msa_client.py" --model protenix --entrypoint "$VENV/bin/protenix" --)
fi
"${protenix_command[@]}" pred --input "$J" --out_dir "$OUT" --seeds 101 \
  --model_name "${MODEL:-protenix_base_default_v1.0.0}" \
  --use_msa true --msa_server_mode colabfold --use_template false "${EXTRA_ARGS[@]}"
# Protenix 2.0.0 catches per-input MSA/inference exceptions and can exit zero
# after logging "Run inference failed". Require an actual prediction artifact.
[ -n "$(find "$OUT" -type f -path '*/predictions/*.cif' -size +0c -print -quit)" ] || {
  echo 'protenix: no predicted CIF produced; inspect the inference/MSA errors above' >&2
  exit 1
}
if [ -z "${BIO_MSA_BUNDLE:-}" ] && [ -z "${BIO_NATIVE_BUNDLE:-}" ]; then
  "$P" "$TOOLS/msa/prepared.py" capture --model protenix --run-dir "$OUT" \
    --out "$OUT/prepared-bundle" --source public --endpoint "$MMSEQS_SERVICE_HOST_URL"
fi
