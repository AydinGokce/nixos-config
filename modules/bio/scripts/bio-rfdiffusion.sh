# bio-rfdiffusion — RFdiffusion backbone generation (diffusion; no MSA/DBs).
#
#   bio-rfdiffusion --contigs '[150-150]' [--num-designs 3] [--out PFX]
#                   [--input-pdb target.pdb] [--hotspots '[A59,A83]'] [-- <hydra overrides>]
#   bio-rfdiffusion raw -- <native scripts/run_inference.py hydra args>
#
# Outputs backbone .pdb (+ .trb metadata) per design. Feed backbones to bio-mpnn
# to get sequences, then fold+`bio-viz`. Trajectories (traj/) animate in PyMOL.

# shellcheck source=/dev/null
[ -r /etc/bio/config.sh ] && source /etc/bio/config.sh
: "${BIO_DATA_DIR:=/opt/bio}"
export LD_LIBRARY_PATH="${BIO_DRIVER_LIB:-/run/opengl-driver/lib}${BIO_WHEEL_LIB:+:$BIO_WHEEL_LIB}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# RFdiffusion's venv uses a uv-managed CPython 3.10, which execs via nix-ld.
if [ -z "${NIX_LD:-}" ] && [ -r /etc/set-environment ]; then
  eval "$(grep -E '^export NIX_LD(_LIBRARY_PATH)?=' /etc/set-environment 2>/dev/null || true)"
fi

venv="$BIO_DATA_DIR/envs/rfdiffusion"
src="$BIO_DATA_DIR/src/rfdiffusion"
models="$BIO_DATA_DIR/weights/rfdiffusion"
if [ ! -x "$venv/bin/python" ] || [ ! -f "$src/scripts/run_inference.py" ]; then
  echo "bio-rfdiffusion: not set up. Run:  bio-setup rfdiffusion" >&2
  exit 3
fi
# shellcheck source=/dev/null
source "$venv/bin/activate"
# torch 1.12 + dgl 1.0 need their bundled CUDA-11 libs (cusparse/curand/cudart).
for _d in "$venv"/lib/python*/site-packages/torch/lib "$venv"/lib/python*/site-packages/nvidia/*/lib; do
  [ -d "$_d" ] && LD_LIBRARY_PATH="$_d:$LD_LIBRARY_PATH"
done
export LD_LIBRARY_PATH
cd "$src" || exit 1

if [ "${1:-}" = "raw" ]; then
  shift; [ "${1:-}" = "--" ] && shift
  exec python scripts/run_inference.py inference.model_directory_path="$models" "$@"
fi

contigs=""; num=3; out="$BIO_DATA_DIR/runs/rfdiffusion/design"; input_pdb=""; hotspots=""
extra=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)     cat <<'USAGE'; exit 0
bio-rfdiffusion — RFdiffusion backbone generation (diffusion; no MSA/DBs).
  bio-rfdiffusion --contigs '[150-150]' [--num-designs 3] [--out PFX]
                  [--input-pdb target.pdb] [--hotspots '[A59,A83]'] [-- <hydra overrides>]
  bio-rfdiffusion raw -- <native scripts/run_inference.py hydra args>
Outputs backbone .pdb (+ .trb) per design; feed to bio-mpnn for sequences.
USAGE
      ;;
    --contigs)     contigs="$2"; shift ;;
    --num-designs) num="$2"; shift ;;
    --out)         out="$2"; shift ;;
    --input-pdb)   input_pdb="$2"; shift ;;
    --hotspots)    hotspots="$2"; shift ;;
    --)            shift; while [ $# -gt 0 ]; do extra+=("$1"); shift; done; break ;;
    *)             extra+=("$1") ;;
  esac
  shift
done

[ -n "$contigs" ] || { echo "bio-rfdiffusion: --contigs is required, e.g. --contigs '[150-150]'" >&2; exit 2; }
mkdir -p "$(dirname "$out")"

set -- "inference.output_prefix=$out" "contigmap.contigs=$contigs" \
       "inference.num_designs=$num" "inference.model_directory_path=$models"
[ -n "$input_pdb" ] && set -- "$@" "inference.input_pdb=$input_pdb"
[ -n "$hotspots" ]  && set -- "$@" "ppi.hotspot_res=$hotspots"

echo "bio-rfdiffusion: contigs=$contigs num=$num -> ${out}_*.pdb" >&2
exec python scripts/run_inference.py "$@" "${extra[@]}"
