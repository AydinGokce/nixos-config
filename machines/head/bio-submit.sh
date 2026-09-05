# bio-submit — run a bio model on an ephemeral DataCrunch GPU node.
#
# Launches a GPU node (FIN-02, same region as the shared NFS), mounts the shared
# filesystem, runs a per-tool RECIPE (machines/head/recipes/<tool>.sh) that builds
# the env + weights on the share (once, cached) and folds/designs, then ALWAYS
# destroys the node. Outputs persist at /mnt/bio-shared/runs/<jobid>/out.
#
#   Folding (no local DBs; remote MSA server or single-seq):
#     bio-submit boltz2     --fasta prot.fasta        [--gpu 1H100.80S.32V] [--spot]
#     bio-submit protenix   --fasta prot.fasta
#     bio-submit openfold3  --fasta prot.fasta
#     bio-submit rfaa       --fasta prot.fasta         # single-sequence, template-free
#     bio-submit af3        --json fold_input.json     # BLOCKED unless gated weights present
#   Design / LM:
#     bio-submit rfdiffusion --contigs '[100-100]' [--num-designs 4] [--input-pdb t.pdb]
#     bio-submit mpnn        --pdb backbone.pdb [--num-seqs 8] [--temp 0.1]
#     bio-submit esm  <score|embed|logits|mutate> --fasta prot.fasta [--model M]
#
# Common flags: --gpu <instance_type> (see `dc types --gpu`), --spot, --model, -- <extra passthrough>.

set -euo pipefail
SHARED_NFS="nfs.fin-02.datacrunch.io:/bio-shared-G523CVN6KYMH"
SHARED_VOL="b8b3b446-e464-44dd-9e01-6402489f8c5a"
SHARED_MNT=/mnt/bio-shared
TOOLS_SRC=/etc/bio-tools
LOC=FIN-02

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

tool="${1:-}"; { [ -n "$tool" ] && shift; } || { usage; exit 2; }
case "$tool" in
  esm|esm2)         recipe=esm;        defgpu=1A100.22V ; inkind=fasta ;;
  mpnn|proteinmpnn) recipe=mpnn;       defgpu=1A6000.10V; inkind=pdb ;;
  rfdiffusion|rfd)  recipe=rfdiffusion;defgpu=1A100.22V ; inkind=optpdb ;;
  rfaa)             recipe=rfaa;       defgpu=1A100.22V ; inkind=fasta ;;
  af3|alphafold3)   recipe=af3;        defgpu=1H100.80S.32V; inkind=json ;;
  boltz2|boltz)     recipe=boltz2;     defgpu=1A100.22V ; inkind=fasta ;;
  protenix)         recipe=protenix;   defgpu=1A100.22V ; inkind=fasta ;;
  openfold3|of3)    recipe=openfold3;  defgpu=1A100.22V ; inkind=fasta ;;
  -h|--help)        usage; exit 0 ;;
  *) echo "bio-submit: unknown tool '$tool'" >&2; usage; exit 2 ;;
esac

gpu="$defgpu"; spot=""; infile=""; model=""; sub=""; contigs=""; num=""; temp=""; name=""; extra=()
# esm's first positional is the subcommand
[ "$recipe" = esm ] && { case "${1:-}" in ""|-*) ;; *) sub="$1"; shift ;; esac; }
while [ $# -gt 0 ]; do
  case "$1" in
    --gpu)          gpu="$2"; shift ;;
    --spot)         spot="--spot" ;;
    --model)        model="$2"; shift ;;
    --fasta|--pdb|--json|--in|--input-pdb) infile="$2"; shift ;;
    --sub)          sub="$2"; shift ;;
    --contigs)      contigs="$2"; shift ;;
    --num-designs|--num|--num-seqs) num="$2"; shift ;;
    --temp)         temp="$2"; shift ;;
    --name)         name="$2"; shift ;;
    --)             shift; while [ $# -gt 0 ]; do extra+=("$1"); shift; done; break ;;
    *)              extra+=("$1") ;;
  esac
  shift
done

jobid="$tool-$(date +%Y%m%d-%H%M%S)"
run="$SHARED_MNT/runs/$jobid"; mkdir -p "$run/in" "$run/out"
RIN=""
if [ -n "$infile" ]; then
  [ -r "$infile" ] || { echo "bio-submit: cannot read input '$infile'" >&2; exit 1; }
  cp "$infile" "$run/in/$(basename "$infile")"; RIN="/mnt/bio-shared/runs/$jobid/in/$(basename "$infile")"
elif [ "$inkind" != optpdb ]; then
  echo "bio-submit: $tool needs an input (--fasta/--pdb/--json/--in)" >&2; exit 2
fi
ROUT="/mnt/bio-shared/runs/$jobid/out"

# sync recipes + tool code + pinned requirements to the shared FS (-L: deref the
# NixOS /etc symlinks so the node gets real files)
mkdir -p "$SHARED_MNT/tools"; rsync -aL --delete "$TOOLS_SRC/" "$SHARED_MNT/tools/"

remote=$(cat <<REMOTE
set -euo pipefail
export HOME=/root PATH=/root/.local/bin:\$PATH
sudo mkdir -p /mnt/bio-shared
need=""
command -v mount.nfs >/dev/null 2>&1 || need="\$need nfs-common"
command -v curl >/dev/null 2>&1 || need="\$need curl"
command -v git  >/dev/null 2>&1 || need="\$need git"
[ -n "\$need" ] && { sudo apt-get update -qq; sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \$need; }
mountpoint -q /mnt/bio-shared || sudo mount -t nfs -o nconnect=16,nolock $SHARED_NFS /mnt/bio-shared
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
export IN='$RIN' OUT='$ROUT' MODEL='$model' SUB='$sub' CONTIGS='$contigs' NUM='$num' TEMP='$temp' NAME='${name:-$tool}'
export EXTRA='${extra[*]:-}'
mkdir -p "$ROUT"
source /mnt/bio-shared/tools/recipes/_common.sh
source /mnt/bio-shared/tools/recipes/$recipe.sh
echo "[bio-submit] done; outputs in $ROUT"; ls -la "$ROUT"
REMOTE
)

echo "bio-submit: job $jobid  tool=$tool gpu=$gpu ${spot:+(spot)}"
echo "bio-submit: results will be at $run/out  (on the shared FS)"
b64=$(printf '%s' "$remote" | base64 -w0)
dc run "$gpu" --loc "$LOC" $spot --volume "$SHARED_VOL" -- "echo $b64 | base64 -d | bash"
echo "bio-submit: DONE — results on the head at $run/out"
ls -la "$run/out" 2>/dev/null || true
