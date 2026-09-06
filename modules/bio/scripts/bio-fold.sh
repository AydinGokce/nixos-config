# bio-fold — from your laptop: run a model on the DataCrunch cluster, fetch the
# result, and optionally view/render it locally in PyMOL. Thin orchestrator over
# the head node's `bio-submit` + a local `bio-viz`.
#
#   bio-fold [model] (--construct REF | --assembly REF | --seq SEQ | --fasta F | --pdb F | --contigs 'STR') [flags]
#     models: boltz2 (default) | protenix | openfold3 | rfaa | rfdiffusion | mpnn | esm | evolvepro
#     --view          open the predicted structure in PyMOL (GUI)
#     --render        save a ray-traced PNG next to the result
#     --out DIR       local output dir (default ~/bio-runs/<jobid>)
#     --gpu TYPE      GPU instance type (else the model default); --spot
#     --sub CMD       esm subcommand (score|embed|logits|mutate)
#     --num N         num designs (rfdiffusion) / seqs (mpnn)
#     -- ...          extra args passed through to the tool
#   examples:
#     bio-fold boltz2 --seq MKTAYIAKQR... --view
#     bio-fold --fasta prot.fasta --render
#     bio-fold boltz2 --construct target-enzyme --render
#     bio-fold protenix --assembly enzyme-oligo --render
#     bio-fold rfdiffusion --contigs '[100-100]' --num 4 --render

# shellcheck source=/dev/null
set -o pipefail
[ ! -r "${BIO_CONFIG_FILE:-/etc/bio/config.sh}" ] || source "${BIO_CONFIG_FILE:-/etc/bio/config.sh}"
HEAD="${BIO_CLUSTER_HEAD:-}"; RUSER="${BIO_CLUSTER_USER:-root}"
KEY="${BIO_CLUSTER_SSHKEY:-$HOME/.ssh/datacrunch_ed25519}"

usage() { cat <<'USAGE'
bio-fold [MODEL] (--construct REF | --assembly REF | --seq SEQUENCE | --fasta FILE | --pdb FILE | --contigs STRING)
  Models: boltz2 (default), openfold3, protenix, rfaa, rfdiffusion, mpnn, esm, evolvepro
  --construct REF        use a construct stored on the head (alias or pinned reference)
  --assembly REF         use an assembly stored on the head
  --render / --view       render PNG / open PyMOL after fetching a structure
  --out DIRECTORY        local results (default ~/bio-runs/JOB)
  --labels FILE          EVOLVEpro measured activity CSV
  --sub COMMAND          ESM command, EVOLVEpro embed|rank, RFAA full|single-seq
  --model NAME           model variant (ESM/EVOLVEpro/MPNN)
  --msa-backend BACKEND   public|private for Boltz2, OpenFold3 or Protenix
  --num N --gpu TYPE --spot --timeout SECONDS -- MODEL_ARGUMENTS
  Import construct/assembly JSON with bio-library import --json FILE, then use its reference.
USAGE
}

[ -n "$HEAD" ] || { echo "bio-fold: no cluster head configured (set bio.cluster.head)" >&2; exit 3; }
[ -r "$KEY" ]  || { echo "bio-fold: ssh key not found: $KEY" >&2; exit 3; }
SSHO=(-i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new)

model=boltz2
case "${1:-}" in -h|--help) usage; exit 0 ;; ""|-*) ;; *) model="$1"; shift ;; esac

seq=""; infile=""; input_option=""; library_ref=""; seq_tmp=""; labels=""; variant=""; seconds=""; contigs=""; num=""; sub=""; gpu=""; spot=""; outdir=""; msa_backend=""; view=0; render=0; extra=()
trap '[ -z "$seq_tmp" ] || rm -f -- "$seq_tmp"' EXIT
while [ $# -gt 0 ]; do
  case "$1" in
    --seq|--fasta|--pdb|--json|--in|--input-pdb|--construct|--assembly|--labels|--model|--timeout|--contigs|--num|--num-designs|--num-seqs|--sub|--gpu|--out|--msa-backend)
      [ $# -ge 2 ] || { echo "bio-fold: $1 needs a value" >&2; exit 2; } ;;
  esac
  case "$1" in
    --seq|--fasta|--pdb|--json|--in|--input-pdb|--construct|--assembly)
      [ -z "$input_option" ] || { echo "bio-fold: choose exactly one sequence, file, construct or assembly input ($input_option conflicts with $1)" >&2; exit 2; }
      [ -n "$2" ] || { echo "bio-fold: $1 needs a nonempty value" >&2; exit 2; }
      input_option="$1" ;;
  esac
  case "$1" in
    --seq)                          seq="$2"; shift ;;
    --fasta|--pdb|--json|--in|--input-pdb) infile="$2"; shift ;;
    --construct|--assembly)          library_ref="$2"; shift ;;
    --labels)                       labels="$2"; shift ;;
    --model)                        variant="$2"; shift ;;
    --timeout)                      seconds="$2"; shift ;;
    --contigs)                      contigs="$2"; shift ;;
    --num|--num-designs|--num-seqs) num="$2"; shift ;;
    --sub)                          sub="$2"; shift ;;
    --gpu)                          gpu="$2"; shift ;;
    --msa-backend)                   msa_backend="$2"; shift ;;
    --spot)                         spot="--spot" ;;
    --out)                          outdir="$2"; shift ;;
    --view|--open)                  view=1 ;;
    --render)                       render=1 ;;
    -h|--help)                      usage; exit 0 ;;
    --)  shift; while [ $# -gt 0 ]; do extra+=("$1"); shift; done; break ;;
    *)   extra+=("$1") ;;
  esac
  shift
done

if [ -n "$library_ref" ] && [ -n "$contigs" ]; then
  echo 'bio-fold: --construct/--assembly cannot be combined with --contigs' >&2; exit 2
fi
if [ -n "$contigs" ] && [ "$model" != rfdiffusion ]; then
  echo 'bio-fold: --contigs is only supported for rfdiffusion' >&2; exit 2
fi
if [ -n "$msa_backend" ]; then
  case "$msa_backend" in public|private) ;; *) echo 'bio-fold: --msa-backend must be public or private' >&2; exit 2 ;; esac
  case "$model" in boltz2|openfold3|protenix) ;; *) echo "bio-fold: --msa-backend is unsupported for $model" >&2; exit 2 ;; esac
fi

rand="$$-$(date +%s)"
rargs=("$model")
stage() { # localfile remoteflag  -> scp to head, append flag+remote-path
  local lf="$1" flag="$2" rp="/tmp/biofold-$rand-${2#--}.${1##*.}"
  [ -r "$lf" ] || { echo "bio-fold: cannot read input '$lf'" >&2; exit 1; }
  scp -q "${SSHO[@]}" "$lf" "$RUSER@$HEAD:$rp" || { echo "bio-fold: scp of input failed" >&2; exit 1; }
  rargs+=("$flag" "$rp")
}

if [ -n "$library_ref" ]; then
  case "$model" in
    boltz2|protenix|openfold3|rfaa|esm|evolvepro) rargs+=("$input_option" "$library_ref") ;;
    *) echo "bio-fold: $model needs a structure input; construct/assembly references are unsupported" >&2; exit 2 ;;
  esac
else
case "$model" in
  boltz2|protenix|openfold3|rfaa|esm|evolvepro)
    if [ -n "$seq" ]; then seq_tmp="$(mktemp --suffix=.fasta)"; infile="$seq_tmp"; printf '>query\n%s\n' "$seq" > "$infile"; fi
    [ -n "$infile" ] || { echo "bio-fold: $model needs --construct, --assembly, --seq or a supported input file" >&2; exit 2; }
    case "$input_option" in
      --json)
        case "$model" in boltz2|protenix|openfold3|rfaa) stage "$infile" --json ;;
          *) echo "bio-fold: $model requires a protein sequence input, not --json" >&2; exit 2 ;;
        esac ;;
      --pdb|--input-pdb) echo "bio-fold: $model requires a sequence or native JSON input, not $input_option" >&2; exit 2 ;;
      *) stage "$infile" --fasta ;;
    esac ;;
  mpnn)
    [ -n "$infile" ] || { echo "bio-fold: mpnn needs --pdb" >&2; exit 2; }
    case "$input_option" in --pdb|--input-pdb|--in) ;; *) echo 'bio-fold: mpnn needs --pdb or --in' >&2; exit 2 ;; esac
    stage "$infile" --pdb ;;
  rfdiffusion)
    [ -n "$contigs" ] || { echo "bio-fold: rfdiffusion needs --contigs" >&2; exit 2; }
    case "$input_option" in ""|--pdb|--input-pdb|--in) ;; *) echo 'bio-fold: rfdiffusion accepts --pdb/--input-pdb with --contigs' >&2; exit 2 ;; esac
    rargs+=(--contigs "$contigs")
    [ -n "$infile" ] && stage "$infile" --input-pdb ;;
  *) echo "bio-fold: unknown model '$model'" >&2; usage; exit 2 ;;
esac
fi
[ -z "$labels" ] || stage "$labels" --labels
[ -z "$sub" ] || rargs+=(--sub "$sub")
[ -z "$variant" ] || rargs+=(--model "$variant")
[ -z "$seconds" ] || rargs+=(--timeout "$seconds")
[ -z "$msa_backend" ] || rargs+=(--msa-backend "$msa_backend")
[ -n "$num" ]  && rargs+=(--num "$num")
[ -n "$gpu" ]  && rargs+=(--gpu "$gpu")
[ -n "$spot" ] && rargs+=("$spot")
[ "${#extra[@]}" -gt 0 ] && rargs+=(-- "${extra[@]}")

# safely-quoted remote command
rc="bio-submit"; for a in "${rargs[@]}"; do rc+=" $(printf '%q' "$a")"; done
echo "bio-fold: $RUSER@$HEAD :: $rc"
log="$(mktemp)"
status=0
ssh "${SSHO[@]}" "$RUSER@$HEAD" "$rc" 2>&1 | tee "$log" || status=$?
[ "$status" -eq 0 ] || { echo "bio-fold: remote job failed ($status); see log $log" >&2; exit "$status"; }
rpath="$(grep -oE 'results at /var/lib/bio-runs/[^ ]+' "$log" | tail -1 | awk '{print $3}')"
[ -n "$rpath" ] || { echo "bio-fold: no result path (the run likely failed — see output above)" >&2; exit 1; }

outdir="${outdir:-$HOME/bio-runs/$(basename "$rpath")}"; mkdir -p "$outdir"
echo "bio-fold: fetching -> $outdir"
rsync -a -e "ssh ${SSHO[*]}" "$RUSER@$HEAD:$rpath/" "$outdir/" || { echo "bio-fold: rsync fetch failed" >&2; exit 1; }

# Retained input bundles contain template structures. Prefer model predictions
# and exclude preparation directories from the fallback used by other models.
struct=""
case "$model" in
  openfold3) struct="$(find "$outdir" -type f -name '*_model.cif' -print -quit 2>/dev/null)" ;;
  boltz2|protenix) struct="$(find "$outdir" -type f -path '*/predictions/*' \( -name '*.pdb' -o -name '*.cif' \) -print -quit 2>/dev/null)" ;;
esac
if [ -z "$struct" ]; then
  struct="$(find "$outdir" -type d \( -name prepared -o -name prepared-bundle -o -name prepared-native -o -name template_data -o -name api-jobs -o -name api-audit \) -prune -o -type f \( -name '*.pdb' -o -name '*.cif' \) -print -quit 2>/dev/null)"
fi
echo "bio-fold: results in $outdir${struct:+  (structure: $struct)}"
if [ -n "$struct" ]; then
  if [ "$render" = 1 ]; then bio-viz --render "$struct" -o "$outdir/render.png" || exit $?; fi
  if [ "$view" = 1 ]; then bio-viz "$struct" || exit $?; fi
elif [ "$render" = 1 ] || [ "$view" = 1 ]; then
  echo "bio-fold: no .pdb/.cif to view (this model outputs sequences/embeddings, not a structure)"
fi
exit 0
