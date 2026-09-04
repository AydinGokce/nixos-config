# bio-mpnn — ProteinMPNN fixed-backbone sequence design.
#
#   bio-mpnn --pdb IN.pdb [--chains A] [--num-seqs 8] [--temp 0.1]
#            [--out DIR] [--model v_48_020] [--ca-only] [--soluble]
#   bio-mpnn raw -- <native protein_mpnn_run.py args>
#
# Output sequences land in <out>/seqs/<name>.fa (FASTA; headers carry score &
# sequence recovery). Designs are sequences only — fold them (ESMFold/AF) to get
# a structure, then `bio-viz` the result.

# shellcheck source=/dev/null
[ -r /etc/bio/config.sh ] && source /etc/bio/config.sh
: "${BIO_DATA_DIR:=/opt/bio}"
export LD_LIBRARY_PATH="${BIO_DRIVER_LIB:-/run/opengl-driver/lib}${BIO_WHEEL_LIB:+:$BIO_WHEEL_LIB}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

venv="$BIO_DATA_DIR/envs/proteinmpnn"
src="$BIO_DATA_DIR/src/proteinmpnn"
if [ ! -x "$venv/bin/python" ] || [ ! -f "$src/protein_mpnn_run.py" ]; then
  echo "bio-mpnn: ProteinMPNN not set up. Run:  bio-setup proteinmpnn" >&2
  exit 3
fi
# shellcheck source=/dev/null
source "$venv/bin/activate"

if [ "${1:-}" = "raw" ]; then
  shift; [ "${1:-}" = "--" ] && shift
  exec python "$src/protein_mpnn_run.py" "$@"
fi

pdb=""; chains=""; num=8; temp="0.1"; out="./mpnn_out"; model="v_48_020"
extra=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)    cat <<'USAGE'; exit 0
bio-mpnn — ProteinMPNN fixed-backbone sequence design.
  bio-mpnn --pdb IN.pdb [--chains A] [--num-seqs 8] [--temp 0.1]
           [--out DIR] [--model v_48_020] [--ca-only] [--soluble]
  bio-mpnn raw -- <native protein_mpnn_run.py args>
Output sequences: <out>/seqs/<name>.fa
USAGE
      ;;
    --pdb)        pdb="$2"; shift ;;
    --chains)     chains="$2"; shift ;;
    --num-seqs)   num="$2"; shift ;;
    --temp)       temp="$2"; shift ;;
    --out)        out="$2"; shift ;;
    --model)      model="$2"; shift ;;
    --ca-only)    extra+=(--ca_only) ;;
    --soluble)    extra+=(--use_soluble_model) ;;
    --)           shift; while [ $# -gt 0 ]; do extra+=("$1"); shift; done; break ;;
    *)            extra+=("$1") ;;
  esac
  shift
done

[ -n "$pdb" ] || { echo "bio-mpnn: --pdb is required (or use: bio-mpnn raw -- ...)" >&2; exit 2; }
[ -r "$pdb" ] || { echo "bio-mpnn: cannot read $pdb" >&2; exit 1; }
mkdir -p "$out"

set -- --pdb_path "$pdb" --out_folder "$out" \
       --num_seq_per_target "$num" --sampling_temp "$temp" \
       --model_name "$model" --batch_size 1 --seed 37
[ -n "$chains" ] && set -- "$@" --pdb_path_chains "$chains"

echo "bio-mpnn: designing $pdb (model $model, $num seqs, T=$temp) -> $out/seqs/" >&2
exec python "$src/protein_mpnn_run.py" "$@" "${extra[@]}"
