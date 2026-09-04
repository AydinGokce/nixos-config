# bio-doctor — diagnose the bio toolkit environment.
# Prints machine config, GPU/driver/nix-ld status, per-tool env + weight
# readiness, and a live torch.cuda check per venv. Makes no changes.

# shellcheck source=/dev/null
[ -r /etc/bio/config.sh ] && source /etc/bio/config.sh
: "${BIO_DATA_DIR:=/opt/bio}"
# nix-ld env for the managed-Python venvs (rfdiffusion/rfaa).
if [ -z "${NIX_LD:-}" ] && [ -r /etc/set-environment ]; then
  eval "$(grep -E '^export NIX_LD(_LIBRARY_PATH)?=' /etc/set-environment 2>/dev/null || true)"
fi

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }

bold "== machine config (/etc/bio/config.sh) =="
printf '  data dir      : %s\n' "$BIO_DATA_DIR"
printf '  gpu vram (GB) : %s\n' "${BIO_GPU_VRAM_GB:-?}"
printf '  esm default   : %s\n' "${BIO_ESM_DEFAULT_MODEL:-?}"

bold "== gpu / driver =="
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader | sed 's/^/  /'
else
  warn "nvidia-smi not found"
fi
if [ -e "${BIO_DRIVER_LIB:-/run/opengl-driver/lib}/libcuda.so.1" ]; then
  ok "driver libcuda.so.1 present"
else
  bad "libcuda.so.1 missing — CUDA wheels will fail"
fi

bold "== nix-ld =="
if [ -n "${NIX_LD:-}" ] || [ -e /lib64/ld-linux-x86-64.so.2 ]; then
  ok "nix-ld active (foreign ELF binaries run; NIX_LD=${NIX_LD:-?})"
else
  warn "nix-ld not detected — managed-Python tools may not run"
fi

# check_tool  LABEL  "venv1 venv2"  srcdir  weight-description
check_tool() {
  local label="$1" venvs="$2" src="$3" wdesc="$4" v py
  printf '  --- %s ---\n' "$label"
  if [ -n "$src" ]; then
    [ -d "$src" ] && ok "source: $src" || warn "source not present: $src"
  fi
  for v in $venvs; do
    if [ -x "$BIO_DATA_DIR/envs/$v/bin/python" ]; then
      py="$("$BIO_DATA_DIR/envs/$v/bin/python" -c 'import sys;print("py%d.%d"%sys.version_info[:2])' 2>/dev/null || echo '?')"
      ok "venv: $v ($py)"
    else
      warn "venv not built: $v"
    fi
  done
  [ -n "$wdesc" ] && printf '    weights: %s\n' "$wdesc"
}

bold "== tools =="
[ "${BIO_ENABLE_ESM:-false}" = true ] && \
  check_tool "ESM-2" "esm2" "" "auto-download to $BIO_DATA_DIR/cache/huggingface"
[ "${BIO_ENABLE_PROTEINMPNN:-false}" = true ] && \
  check_tool "ProteinMPNN" "proteinmpnn" "$BIO_DATA_DIR/src/proteinmpnn" "bundled in repo"
[ "${BIO_ENABLE_EVOLVEPRO:-false}" = true ] && \
  check_tool "EVOLVEpro" "evolvepro-core evolvepro-plm" "$BIO_DATA_DIR/src/evolvepro" "none (uses ESM weights)"
[ "${BIO_ENABLE_RFDIFFUSION:-false}" = true ] && \
  check_tool "RFdiffusion" "rfdiffusion" "$BIO_DATA_DIR/src/rfdiffusion" \
    "$(du -sh "$BIO_DATA_DIR/weights/rfdiffusion" 2>/dev/null | cut -f1) in weights/rfdiffusion"
[ "${BIO_ENABLE_RFAA:-false}" = true ] && \
  check_tool "RoseTTAFold-All-Atom" "rfaa" "$BIO_DATA_DIR/src/rfaa" \
    "$( [ -f "$BIO_DATA_DIR/src/rfaa/RFAA_paper_weights.pt" ] && echo 'RFAA_paper_weights.pt present' || echo 'RFAA_paper_weights.pt missing' )"
[ "${BIO_ENABLE_AF3:-false}" = true ] && \
  check_tool "AlphaFold 3" "alphafold3" "$BIO_DATA_DIR/src/alphafold3" \
    "GATED — drop af3.bin in weights/alphafold3 (see README)"

bold "== live torch.cuda check per venv =="
for v in esm2 proteinmpnn evolvepro-plm rfdiffusion rfaa; do
  venv="$BIO_DATA_DIR/envs/$v"
  [ -x "$venv/bin/python" ] || continue
  ld="${BIO_DRIVER_LIB:-/run/opengl-driver/lib}:${BIO_WHEEL_LIB:-}"
  for d in "$venv"/lib/python*/site-packages/torch/lib "$venv"/lib/python*/site-packages/nvidia/*/lib; do
    [ -d "$d" ] && ld="$d:$ld"
  done
  if out=$(LD_LIBRARY_PATH="$ld" "$venv/bin/python" -c \
      'import torch;print(torch.__version__, "cuda="+str(torch.cuda.is_available()))' 2>/dev/null); then
    ok "$v: torch $out"
  else
    warn "$v: torch check failed"
  fi
done

bold "== disk =="
df -h "$BIO_DATA_DIR" 2>/dev/null | sed 's/^/  /'
du -sh "$BIO_DATA_DIR"/* 2>/dev/null | sed 's/^/  /' || true
