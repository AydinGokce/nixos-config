# bio-af3 — AlphaFold 3 front-end.
#
#   bio-af3 --json IN.json --out DIR [--no-data-pipeline] [-- <run_alphafold.py args>]
#   bio-af3 raw -- <native run_alphafold.py args>
#
# BLOCKED on this machine (see modules/bio/README.md): model weights are GATED
# (request from Google + EULA), the genetic databases are ~630GB (won't fit), and
# 8GB VRAM is below AF3's ~16GB floor. The code + C++ data pipeline are installed
# so imports and the CPU data pipeline work on tiny precomputed-MSA inputs.
# Place approved weights at $BIO_DATA_DIR/weights/alphafold3 (af3.bin) to use them.

# shellcheck source=/dev/null
[ -r /etc/bio/config.sh ] && source /etc/bio/config.sh
: "${BIO_DATA_DIR:=/opt/bio}"
export LD_LIBRARY_PATH="${BIO_DRIVER_LIB:-/run/opengl-driver/lib}${BIO_WHEEL_LIB:+:$BIO_WHEEL_LIB}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# unified-memory knobs so an 8GB card at least attempts to spill to host RAM
export XLA_PYTHON_CLIENT_PREALLOCATE="${XLA_PYTHON_CLIENT_PREALLOCATE:-false}"
export TF_FORCE_UNIFIED_MEMORY="${TF_FORCE_UNIFIED_MEMORY:-true}"

venv="$BIO_DATA_DIR/envs/alphafold3"
src="$BIO_DATA_DIR/src/alphafold3"
model_dir="$BIO_DATA_DIR/weights/alphafold3"
db_dir="$BIO_DATA_DIR/weights/alphafold3-dbs"
if [ ! -x "$venv/bin/python" ] || [ ! -f "$src/run_alphafold.py" ]; then
  echo "bio-af3: not set up. Run:  bio-setup alphafold3" >&2
  exit 3
fi
# shellcheck source=/dev/null
source "$venv/bin/activate"
cd "$src" || exit 1

if [ "${1:-}" = "raw" ]; then
  shift; [ "${1:-}" = "--" ] && shift
  exec python run_alphafold.py "$@"
fi

json=""; out="$BIO_DATA_DIR/runs/af3"; run_dp=1
extra=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)          cat <<'USAGE'; exit 0
bio-af3 — AlphaFold 3 front-end.
  bio-af3 --json IN.json --out DIR [--no-data-pipeline] [-- <run_alphafold.py args>]
  bio-af3 raw -- <native run_alphafold.py args>
BLOCKED here: gated weights (request from Google + EULA), ~630GB DBs won't fit,
8GB VRAM < ~16GB floor. Place approved af3.bin at $BIO_DATA_DIR/weights/alphafold3.
USAGE
      ;;
    --json)             json="$2"; shift ;;
    --out)              out="$2"; shift ;;
    --no-data-pipeline) run_dp=0 ;;
    --)                 shift; while [ $# -gt 0 ]; do extra+=("$1"); shift; done; break ;;
    *)                  extra+=("$1") ;;
  esac
  shift
done

if [ ! -f "$model_dir/af3.bin" ] && [ ! -f "$model_dir/af3.bin.zst" ]; then
  echo "bio-af3: no model parameters at $model_dir (af3.bin)." >&2
  echo "         AF3 weights are gated: request access at" >&2
  echo "         https://github.com/google-deepmind/alphafold3 (see README 'Obtaining Model Parameters')," >&2
  echo "         accept the EULA, then drop af3.bin into $model_dir." >&2
  exit 4
fi
[ -n "$json" ] || { echo "bio-af3: --json is required" >&2; exit 2; }
mkdir -p "$out"

set -- --json_path="$json" --model_dir="$model_dir" --output_dir="$out"
[ -d "$db_dir" ] && set -- "$@" --db_dir="$db_dir"
[ "$run_dp" -eq 0 ] && set -- "$@" --run_data_pipeline=false

echo "bio-af3: running run_alphafold.py -> $out" >&2
exec python run_alphafold.py "$@" "${extra[@]}"
