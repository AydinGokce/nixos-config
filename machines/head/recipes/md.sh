# The runtime is prepared once, pinned and restored on each temporary worker.
# GROMACS writes results/checkpoints to the persistent per-job output directory.
set -euo pipefail
variant=${BIO_MD_VARIANT:?Expected worker variant must be supplied by bio-submit}
case "$variant" in
  cpu) ;;
  cuda) nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null || {
    echo 'MD GPU worker has no working NVIDIA device; refusing an implicit CPU fallback' >&2
    exit 2
  } ;;
  *) echo 'Unsupported MD runtime variant' >&2; exit 2 ;;
esac
export PYTHONPATH="$BIO_TOOLS_DIR"
python3 -m md.launch --bundle "$IN" --out "$OUT" --variant "$variant"
