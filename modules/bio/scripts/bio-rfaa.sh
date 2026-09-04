# bio-rfaa — RoseTTAFold-All-Atom structure prediction.
#
#   bio-rfaa --fasta target.fasta [--name job] [--out DIR] [-- <hydra overrides>]
#   bio-rfaa raw -- <native `python -m rf2aa.run_inference` args>
#
# NOTE (this machine): the ~399GB MSA databases are NOT installed and signalp6 is
# gated, so bio-rfaa runs in single-sequence mode (accuracy is reduced vs. a full
# MSA). Best for small single chains (<~500 aa) on 8GB. Output is a .pdb with
# per-atom pLDDT in the B-factor column — open with `bio-viz`.

# shellcheck source=/dev/null
[ -r /etc/bio/config.sh ] && source /etc/bio/config.sh
: "${BIO_DATA_DIR:=/opt/bio}"
export LD_LIBRARY_PATH="${BIO_DRIVER_LIB:-/run/opengl-driver/lib}${BIO_WHEEL_LIB:+:$BIO_WHEEL_LIB}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export WANDB_MODE="${WANDB_MODE:-disabled}"
# torch 2.0.1 (RFAA's torch) predates the 'expandable_segments' allocator option.
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-max_split_size_mb:512}"
# RFAA's venv uses a uv-managed CPython 3.10, which execs via nix-ld.
if [ -z "${NIX_LD:-}" ] && [ -r /etc/set-environment ]; then
  eval "$(grep -E '^export NIX_LD(_LIBRARY_PATH)?=' /etc/set-environment 2>/dev/null || true)"
fi

venv="$BIO_DATA_DIR/envs/rfaa"
src="$BIO_DATA_DIR/src/rfaa"
weights="$src/RFAA_paper_weights.pt"
if [ ! -x "$venv/bin/python" ] || [ ! -d "$src/rf2aa" ]; then
  echo "bio-rfaa: not set up. Run:  bio-setup rfaa" >&2
  exit 3
fi
[ -f "$weights" ] || echo "bio-rfaa: warning: weights $weights missing (bio-setup rfaa downloads them)" >&2
# shellcheck source=/dev/null
source "$venv/bin/activate"
# torch + dgl need their bundled CUDA libs (cusparse/curand/cudart) on the path.
for _d in "$venv"/lib/python*/site-packages/torch/lib "$venv"/lib/python*/site-packages/nvidia/*/lib; do
  [ -d "$_d" ] && LD_LIBRARY_PATH="$_d:$LD_LIBRARY_PATH"
done
export LD_LIBRARY_PATH
cd "$src" || exit 1

if [ "${1:-}" = "raw" ]; then
  shift; [ "${1:-}" = "--" ] && shift
  exec python -m rf2aa.run_inference "$@"
fi

fasta=""; name="rfaa_job"; out="$BIO_DATA_DIR/runs/rfaa"; a3m=""
extra=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)  cat <<'USAGE'; exit 0
bio-rfaa — RoseTTAFold-All-Atom structure prediction (single-sequence mode here).
  bio-rfaa --fasta target.fasta [--name job] [--out DIR] [--a3m msa.a3m] [-- <hydra overrides>]
  bio-rfaa raw -- <native `python -m rf2aa.run_inference` args>
NOTE: the ~399GB MSA + ~81GB template DBs are NOT installed and signalp6 is
gated, so this runs SINGLE-SEQUENCE, TEMPLATE-FREE by default (a query-only a3m
is staged automatically). Pass --a3m to supply a real precomputed MSA. Best for
small single chains (<~500 aa) on 8GB. Output .pdb (per-atom pLDDT in B-factor).
USAGE
      ;;
    --fasta)    fasta="$2"; shift ;;
    --name)     name="$2"; shift ;;
    --out)      out="$2"; shift ;;
    --a3m)      a3m="$2"; shift ;;
    --)         shift; while [ $# -gt 0 ]; do extra+=("$1"); shift; done; break ;;
    *)          extra+=("$1") ;;
  esac
  shift
done

[ -n "$fasta" ] || { echo "bio-rfaa: --fasta is required (or use: bio-rfaa raw -- ...)" >&2; exit 2; }
[ -r "$fasta" ] || { echo "bio-rfaa: cannot read $fasta" >&2; exit 1; }
mkdir -p "$out"

# Stage the MSA so RFAA's make_msa skips its DB search (we have no MSA/template
# DBs). Default: a query-only a3m + empty template files -> single-sequence,
# template-free. With --a3m, use the supplied MSA instead.
chaindir="$out/$name/A"
mkdir -p "$chaindir"
if [ -n "$a3m" ]; then
  cp "$a3m" "$chaindir/t000_.msa0.a3m"
elif [ ! -s "$chaindir/t000_.msa0.a3m" ]; then
  { printf '>query\n'; grep -v '^>' "$fasta" | tr -d '\n\r \t'; printf '\n'; } > "$chaindir/t000_.msa0.a3m"
fi
[ -f "$chaindir/t000_.atab" ] || : > "$chaindir/t000_.atab"
[ -f "$chaindir/t000_.hhr" ]  || : > "$chaindir/t000_.hhr"

echo "bio-rfaa: predicting $fasta ${a3m:+(MSA: $a3m) }-> $out/$name.pdb" >&2
exec python -m rf2aa.run_inference --config-name protein \
    "job_name=$name" "output_path=$out" "checkpoint_path=$weights" \
    "protein_inputs.A.fasta_file=$fasta" "${extra[@]}"
