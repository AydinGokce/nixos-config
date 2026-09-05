# bio-submit — run a bio model on an ephemeral DataCrunch GPU node.
#
# Launches a GPU node (FIN-02, same region as the shared NFS), mounts the shared
# filesystem, builds the tool's env on it (once — cached across jobs), runs the
# model, and ALWAYS destroys the node (via `dc run`'s trap). Outputs persist on
# the shared FS at /mnt/bio-shared/runs/<jobid> (readable here on the head).
#
#   bio-submit esm  <score|embed|logits|mutate> --fasta F [--model M] [--gpu T] [--spot] [-- extra]
#   bio-submit mpnn --pdb P [--chains A] [--num-seqs N] [--temp 0.1] [--gpu T] [--spot]
#
# GPU defaults per tool (override with --gpu <instance_type>; see `dc types --gpu`).
# Supported now: esm, mpnn (pure torch-cu124). rfdiffusion/rfaa/af3 come later
# (they need the DGL/CUDA-11 dance and, for rfaa/af3, the large databases).

set -euo pipefail
SHARED_NFS="nfs.fin-02.datacrunch.io:/bio-shared-G523CVN6KYMH"
SHARED_VOL="b8b3b446-e464-44dd-9e01-6402489f8c5a"   # DataCrunch NVMe_Shared volume id
SHARED_MNT=/mnt/bio-shared
TOOLS_SRC=/etc/bio-tools
LOC=FIN-02

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }

tool="${1:-}"; { [ -n "$tool" ] && shift; } || { usage; exit 2; }
case "$tool" in
  esm|esm2)         env=esm2;        pyver=3.11; reqs=esm2.txt;        defgpu=1A100.22V ;;
  mpnn|proteinmpnn) env=proteinmpnn; pyver=3.11; reqs=proteinmpnn.txt; defgpu=1A6000.10V ;;
  -h|--help)        usage; exit 0 ;;
  *) echo "bio-submit: unsupported tool '$tool' (esm|mpnn)" >&2; usage; exit 2 ;;
esac

gpu="$defgpu"; spot=""; fasta=""; pdb=""; model=""; sub=""; extra=()
# esm's first positional is the subcommand
if [ "$env" = esm2 ]; then sub="${1:-score}"; [ $# -gt 0 ] && shift || true; fi
while [ $# -gt 0 ]; do
  case "$1" in
    --gpu)   gpu="$2"; shift ;;
    --spot)  spot="--spot" ;;
    --fasta) fasta="$2"; shift ;;
    --pdb)   pdb="$2"; shift ;;
    --model) model="$2"; shift ;;
    --)      shift; while [ $# -gt 0 ]; do extra+=("$1"); shift; done; break ;;
    *)       extra+=("$1") ;;
  esac
  shift
done

jobid="$tool-$(date +%Y%m%d-%H%M%S)"
run="$SHARED_MNT/runs/$jobid"
mkdir -p "$run/in" "$run/out"
# stage input onto the shared FS (head has it mounted)
case "$env" in
  esm2)        [ -n "$fasta" ] || { echo "bio-submit esm: --fasta required" >&2; exit 2; }
               cp "$fasta" "$run/in/in.fasta"; IN="$run/in/in.fasta" ;;
  proteinmpnn) [ -n "$pdb" ] || { echo "bio-submit mpnn: --pdb required" >&2; exit 2; }
               cp "$pdb" "$run/in/in.pdb"; IN="$run/in/in.pdb" ;;
esac
# sync tool code (py CLIs + pinned requirements) to the shared FS.
# -L dereferences symlinks: /etc/bio-tools/* are NixOS symlinks into /etc/static,
# so we must copy the real file contents (the GPU node has no /etc/static).
mkdir -p "$SHARED_MNT/tools"; rsync -aL --delete "$TOOLS_SRC/" "$SHARED_MNT/tools/"

# remote input/output paths are the same (shared mount)
RIN="/mnt/bio-shared/runs/$jobid/in/$(basename "$IN")"
ROUT="/mnt/bio-shared/runs/$jobid/out"

# per-tool run command (executed inside the venv on the GPU node)
modelarg=""; [ -n "$model" ] && modelarg="--model $model"
case "$env" in
  esm2)        runcmd="python /mnt/bio-shared/tools/py/esm_cli.py $sub -i '$RIN' -o '$ROUT/result' $modelarg ${extra[*]:-}" ;;
  proteinmpnn) runcmd="python \$SRC/protein_mpnn_run.py --pdb_path '$RIN' --out_folder '$ROUT' --num_seq_per_target 8 --sampling_temp 0.1 --model_name v_48_020 --batch_size 1 --seed 37 ${extra[*]:-}" ;;
esac

# build the remote bootstrap+run script
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
export HF_HOME=/mnt/bio-shared/cache/hf TORCH_HOME=/mnt/bio-shared/cache/torch
VENV=/mnt/bio-shared/envs/$env
# Build against the node's SYSTEM python3 (identical across the Ubuntu image), not
# a uv-managed interpreter — a managed python lives on node-local disk, so a venv
# on the shared FS would have a dangling interpreter on the next ephemeral node.
PYSYS="\$(command -v python3)"
if ! "\$VENV/bin/python" -c '' >/dev/null 2>&1; then
  echo "[bio-submit] creating env $env (system python: \$PYSYS)"
  rm -rf "\$VENV"; uv venv --python "\$PYSYS" "\$VENV"
fi
echo "[bio-submit] ensuring env $env deps (idempotent) ..."
uv pip install --python "\$VENV/bin/python" -r /mnt/bio-shared/tools/requirements/$reqs
SRC=/mnt/bio-shared/src/proteinmpnn
if [ "$env" = proteinmpnn ] && [ ! -f "\$SRC/protein_mpnn_run.py" ]; then
  git clone --depth 1 https://github.com/dauparas/ProteinMPNN "\$SRC"
fi
source "\$VENV/bin/activate"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true
mkdir -p "$ROUT"
echo "[bio-submit] running $tool ..."
$runcmd
echo "[bio-submit] done; outputs in $ROUT"
ls -la "$ROUT"
REMOTE
)

echo "bio-submit: job $jobid  tool=$tool gpu=$gpu ${spot:+(spot)}"
echo "bio-submit: results will be at $run/out  (on the shared FS)"
# base64 the script so it survives SSH's remote word-splitting as a single token
b64=$(printf '%s' "$remote" | base64 -w0)
dc run "$gpu" --loc "$LOC" $spot --volume "$SHARED_VOL" -- "echo $b64 | base64 -d | bash"
echo "bio-submit: DONE — results on the head at $run/out"
ls -la "$run/out" 2>/dev/null || true
