# Boltz-2 — AF3-class folding (MIT; commercial OK). Weights auto-download (~7.6GB)
# to the shared cache. No local DBs: MSAs come from the remote ColabFold server
# (--use_msa_server). IN = query FASTA (single protein chain).
if [ -n "${BIO_MSA_BUNDLE:-}" ]; then
  python3 "$TOOLS/msa/prepared.py" validate --bundle "$BIO_MSA_BUNDLE" --model boltz2 --fasta "$IN"
  for arg in "${EXTRA_ARGS[@]}"; do
    case "$arg" in
      --use_msa_server*|--msa_*|--templates*)
        echo "boltz2: prepared input conflicts with $arg" >&2; exit 2 ;;
    esac
  done
fi
VENV="$SHARED/envs/boltz"; export BOLTZ_CACHE="$SHARED/cache/boltz"; mkdir -p "$BOLTZ_CACHE"
sys_venv "$VENV"; P="$VENV/bin/python"
# Boltz's open-ended dependencies now select CUDA 13. Keep this CUDA-12.8
# image on a fixed stack; Torch 2.8 supplies Triton 3.4 for Blackwell kernels.
uv pip install --python "$P" torch==2.8.0+cu128 \
  --index-url https://download.pytorch.org/whl/cu128
uv pip install --python "$P" 'boltz[cuda]==2.2.1' torch==2.8.0 triton==3.4.0 \
  cuequivariance==0.6.1 cuequivariance-torch==0.6.1 \
  cuequivariance-ops-cu12==0.6.1 cuequivariance-ops-torch-cu12==0.6.1
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
have_gpu
"$P" - "${EXTRA_ARGS[@]}" <<'PY'
from importlib.metadata import version
import os
import sys

expected = {
    "boltz": "2.2.1", "torch": "2.8.0+cu128", "triton": "3.4.0",
    "cuequivariance": "0.6.1", "cuequivariance-torch": "0.6.1",
    "cuequivariance-ops-cu12": "0.6.1", "cuequivariance-ops-torch-cu12": "0.6.1",
}
for package, wanted in expected.items():
    installed = version(package)
    if installed != wanted:
        sys.exit(f"Boltz runtime mismatch: {package}={installed}, expected {wanted}")

import torch

if torch.version.cuda != "12.8" or not torch.cuda.is_available():
    sys.exit(f"Boltz requires working CUDA 12.8 Torch; built CUDA={torch.version.cuda}")
if not torch.cuda.is_bf16_supported():
    sys.exit("Boltz-2 requires a GPU supporting its bfloat16 inference precision")
with torch.inference_mode():
    x = torch.ones((16, 16), device="cuda")
    if not torch.isfinite(x @ x).all().item():
        sys.exit("Boltz CUDA matrix multiplication returned non-finite values")
torch.cuda.synchronize()
print(f"Boltz runtime verified: {expected}; GPU={torch.cuda.get_device_name()}", flush=True)

if "--no_kernels" in sys.argv[1:]:
    print("Boltz optimized kernel preflight skipped for explicit --no_kernels", flush=True)
else:
    # Probe only: force optimized paths even if the caller changes fallback
    # thresholds. These process-local settings do not alter prediction options.
    os.environ["CUEQ_TRIMUL_FALLBACK_THRESHOLD"] = "0"
    os.environ["CUEQ_TRIATTN_FALLBACK_THRESHOLD"] = "0"
    from boltz.model.layers.triangular_mult import (
        TriangleMultiplicationIncoming, TriangleMultiplicationOutgoing,
    )
    from boltz.model.layers.triangular_attention.attention import TriangleAttention

    # 128 tokens and attention head dim 32 also exceed the default small-input
    # fallback. Exercise Boltz's actual layers before downloads/MSA/prediction.
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
        x = torch.randn((1, 128, 128, 128), device="cuda")
        mask = torch.ones((1, 128, 128), device="cuda")
        for layer in (TriangleMultiplicationOutgoing(128),
                      TriangleMultiplicationIncoming(128),
                      TriangleAttention(c_in=128, c_hidden=32, no_heads=4)):
            layer = layer.cuda().eval()
            result = layer(x, mask, use_kernels=True)
            torch.cuda.synchronize()
            if result.shape != x.shape or not result.is_cuda or not torch.isfinite(result).all().item():
                sys.exit(f"Boltz kernel preflight failed: {type(layer).__name__}")
            print(f"Boltz CUDA kernel passed: {type(layer).__name__}", flush=True)
PY
SEQ=$(grep -v '^>' "$IN" | tr -d '\n\r \t')
YAML="$OUT/input.yaml"
msa_args=(--use_msa_server)
if [ -n "${BIO_MSA_BUNDLE:-}" ]; then
  YAML=$("$P" "$TOOLS/msa/prepared.py" materialize --bundle "$BIO_MSA_BUNDLE" \
      --model boltz2 --fasta "$IN" --out "$OUT/prepared-native")
  msa_args=()
else
cp "$IN" "$OUT/reference_input.fasta"
cat > "$YAML" <<YAML
version: 1
sequences:
  - protein:
      id: A
      sequence: $SEQ
YAML
fi
# shellcheck disable=SC2086
"$VENV/bin/boltz" predict "$YAML" "${msa_args[@]}" --accelerator gpu --devices 1 \
  --out_dir "$OUT" --output_format pdb --cache "$BOLTZ_CACHE" "${EXTRA_ARGS[@]}"
# Boltz catches preprocessing and prediction failures and can still exit zero.
# Input files and intermediate caches are not evidence of a completed structure.
"$VENV/bin/python" - "$OUT" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1]) / "boltz_results_input" / "predictions" / "input"
structures = [p for p in root.glob("input_model_*")
              if p.suffix in {".pdb", ".cif"} and p.is_file() and p.stat().st_size > 0]
if not structures:
    sys.exit("Boltz finished without a predicted PDB/CIF; inspect preprocessing/prediction errors above")
print(f"Boltz completed {len(structures)} predicted structure(s)")
PY
if [ -z "${BIO_MSA_BUNDLE:-}" ]; then
  "$P" "$TOOLS/msa/prepared.py" capture --model boltz2 --run-dir "$OUT" \
    --out "$OUT/prepared-bundle" --source public --endpoint https://api.colabfold.com
fi
