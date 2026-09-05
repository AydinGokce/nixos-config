# _common.sh — helpers sourced by every bio-submit tool recipe.
# Recipes run ON the ephemeral GPU node (stock Ubuntu 24.04 + CUDA + Docker) with
# the shared NFS mounted at /mnt/bio-shared. Each recipe is invoked with env vars:
#   IN    = staged input path on the shared FS (or empty)
#   OUT   = output dir on the shared FS (already created)
#   EXTRA = passthrough extra args (string)
#   MODEL = optional model name; SUB = optional subcommand
# and must build/reuse its env + weights on the shared FS, then write to $OUT.
set -euo pipefail
SHARED=/mnt/bio-shared
export HOME=/root
export PATH=/root/.local/bin:$PATH
export HF_HOME="$SHARED/cache/hf" TORCH_HOME="$SHARED/cache/torch" UV_CACHE_DIR="$SHARED/cache/uv"
mkdir -p "$HF_HOME" "$TORCH_HOME" "$UV_CACHE_DIR"

# CUDA-11 runtime libs (dgl/old-torch) + a venv's torch libs on LD_LIBRARY_PATH.
venv_ld() { # venvdir
  local v="$1" sp d out=""
  sp=$(echo "$v"/lib/python*/site-packages 2>/dev/null) || return 0
  for d in "$sp"/torch/lib "$sp"/nvidia/*/lib; do [ -d "$d" ] && out="$d:$out"; done
  printf '%s' "$out"
}

# Portable venv built against the node's SYSTEM python3 (same across the image),
# so the venv on the shared FS works on any ephemeral node. Rebuild if broken.
sys_venv() { # venvdir
  local v="$1"
  "$v/bin/python" -c '' >/dev/null 2>&1 || { rm -rf "$v"; uv venv --python "$(command -v python3)" "$v"; }
}

# A relocatable CPython 3.10 living ON the shared FS (for old-torch tools whose
# wheels have no cp312). python-build-standalone is relocatable, so copying the
# uv-managed interpreter onto the share makes venvs pointing at it portable.
nfs_py310() {
  local root="$SHARED/python/py310" py="$SHARED/python/py310/bin/python3.10"
  if ! "$py" -c '' >/dev/null 2>&1; then
    uv python install 3.10 >/dev/null 2>&1 || true
    local found; found=$(uv python find 3.10)
    local src; src=$(dirname "$(dirname "$found")")
    rm -rf "$root"; mkdir -p "$(dirname "$root")"; cp -a "$src" "$root"
  fi
  printf '%s' "$py"
}

dl() { mkdir -p "$(dirname "$2")"; wget -q -c -O "$2" "$1"; }
have_gpu() { nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true; }
