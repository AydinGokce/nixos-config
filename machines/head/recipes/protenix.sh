# Protenix (ByteDance AF3 reproduction; Apache-2.0, commercial OK). Weights
# auto-download. Uses the remote ColabFold MSA server. IN = query FASTA.
# Uses torch 2.7.1+cu128 and the default cuEquivariance kernels. The CUDA image
# also provides nvcc for optional JIT kernels. Weights download can be slow.
VENV="$SHARED/envs/protenix"
# protenix 2.0.0 reads PROTENIX_ROOT_DIR (NOT ..._DATA_...) and keeps checkpoints
# in $PROTENIX_ROOT_DIR/checkpoint; unset it and weights re-download from the slow
# Beijing host onto the ephemeral node's disk on EVERY run.
export PROTENIX_ROOT_DIR="$SHARED/protenix/release_data"
mkdir -p "$PROTENIX_ROOT_DIR" "$TORCH_HOME" "$HF_HOME"
sys_venv "$VENV"; P="$VENV/bin/python"
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
SEQ=$(grep -v '^>' "$IN" | tr -d '\n\r \t'); JOB="${NAME:-protenix_job}"
J="$OUT/input.json"
cat > "$J" <<JSON
[ { "name": "$JOB", "sequences": [ { "proteinChain": { "sequence": "$SEQ", "count": 1 } } ] } ]
JSON
have_gpu
# shellcheck disable=SC2086
"$VENV/bin/protenix" predict --input "$J" --out_dir "$OUT" --seeds 101 \
  --model_name "${MODEL:-protenix_base_default_v1.0.0}" \
  --use_msa true --msa_server_mode colabfold --use_template false "${EXTRA_ARGS[@]}"
# Protenix 2.0.0 catches per-input MSA/inference exceptions and can exit zero
# after logging "Run inference failed". Require an actual prediction artifact.
[ -n "$(find "$OUT" -type f -path '*/predictions/*.cif' -size +0c -print -quit)" ] || {
  echo 'protenix: no predicted CIF produced; inspect the inference/MSA errors above' >&2
  exit 1
}
