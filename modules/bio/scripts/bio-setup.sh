# bio-setup — materialize per-tool uv venvs, source repos, and ungated weights.
#
#   bio-setup all                 set up every enabled tool
#   bio-setup esm2 proteinmpnn    set up specific tools
#   bio-setup rfdiffusion --force rebuild a tool's venv from scratch
#
# Tools: proteinmpnn esm2 evolvepro rfdiffusion rfaa alphafold3
# Idempotent: existing venvs are reused unless --force. Everything lands under
# $BIO_DATA_DIR (default /opt/bio): src/, envs/, weights/, logs/.
#
# Deliberately NOT fetched: AlphaFold 3 weights (gated) + its ~630GB DBs, RFAA's
# ~399GB MSA DBs, and signalp6 (gated). See modules/bio/README.md.

set +e  # this is a long installer; we track per-tool status instead of aborting

# shellcheck source=/dev/null
[ -r /etc/bio/config.sh ] && source /etc/bio/config.sh
: "${BIO_DATA_DIR:=/opt/bio}"
: "${BIO_ESM_DEFAULT_MODEL:=esm2_t33_650M_UR50D}"
export LD_LIBRARY_PATH="${BIO_DRIVER_LIB:-/run/opengl-driver/lib}${BIO_WHEEL_LIB:+:$BIO_WHEEL_LIB}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# uv-managed CPython 3.10 (RFdiffusion/RFAA) execs via nix-ld; pull NIX_LD* from
# the session env file without sourcing it whole (which would reset PATH).
if [ -z "${NIX_LD:-}" ] && [ -r /etc/set-environment ]; then
  eval "$(grep -E '^export NIX_LD(_LIBRARY_PATH)?=' /etc/set-environment 2>/dev/null || true)"
fi
export HF_HOME="${HF_HOME:-$BIO_DATA_DIR/cache/huggingface}"
export TORCH_HOME="${TORCH_HOME:-$BIO_DATA_DIR/cache/torch}"
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"

SRC="$BIO_DATA_DIR/src"
ENVS="$BIO_DATA_DIR/envs"
WTS="$BIO_DATA_DIR/weights"

# ---- pinned refs (from the cross-checked research recipes) --------------------
MPNN_URL=https://github.com/dauparas/ProteinMPNN;            MPNN_REF=8907e6671bfbfc92303b5f79c4b5e6ce47cdef57
ESM_URL=https://github.com/facebookresearch/esm;            ESM_REF=main
EVO_URL=https://github.com/mat10d/EvolvePro;                EVO_REF=1c77697d0c09bf6989a1562a55da99301a12e2cd
RFD_URL=https://github.com/RosettaCommons/RFdiffusion;      RFD_REF=86507b6538f51fce57b5a72477165f03999ed7ae
RFAA_URL=https://github.com/baker-laboratory/RoseTTAFold-All-Atom; RFAA_REF=d69ab3a73f8ede31a4cc005fbc076a341d848469
AF3_URL=https://github.com/google-deepmind/alphafold3;      AF3_REF=85c4d20505fd5cef05eac22b534d4e793971ae69

usage() { cat <<'USAGE'
bio-setup — materialize per-tool uv venvs, source repos, and ungated weights.
  bio-setup all                 set up every enabled tool
  bio-setup esm2 proteinmpnn    set up specific tools
  bio-setup rfdiffusion --force rebuild a tool's venv from scratch
Tools: proteinmpnn esm2 evolvepro rfdiffusion rfaa alphafold3
NOT fetched: AF3 weights (gated) + ~630GB DBs, RFAA ~399GB DBs, signalp6 (gated).
USAGE
}

FORCE=""
TARGETS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --force)   FORCE=1 ;;
    all)       TARGETS=(proteinmpnn esm2 evolvepro rfdiffusion rfaa alphafold3) ;;
    proteinmpnn|mpnn) TARGETS+=(proteinmpnn) ;;
    esm2|esm)  TARGETS+=(esm2) ;;
    evolvepro) TARGETS+=(evolvepro) ;;
    rfdiffusion|rfd) TARGETS+=(rfdiffusion) ;;
    rfaa)      TARGETS+=(rfaa) ;;
    alphafold3|af3) TARGETS+=(alphafold3) ;;
    *) echo "bio-setup: unknown target '$1'" >&2; exit 2 ;;
  esac
  shift
done
[ "${#TARGETS[@]}" -gt 0 ] || { usage; exit 2; }

mkdir -p "$SRC" "$ENVS" "$WTS" "$BIO_DATA_DIR/logs" "$BIO_DATA_DIR/runs" "$HF_HOME" "$TORCH_HOME"
# Run from a neutral dir so uv doesn't discover an unrelated pyproject.toml in
# the caller's CWD and impose its python/requires constraints on our installs.
cd "$BIO_DATA_DIR" 2>/dev/null || cd /tmp || true
LOG="$BIO_DATA_DIR/logs/setup-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "bio-setup: log -> $LOG"

log()  { printf '\n\033[1m[bio-setup] %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[bio-setup] WARN: %s\033[0m\n' "$*" >&2; }

declare -A STATUS

clone_pin() { # url ref dest [--recurse-submodules]
  local url="$1" ref="$2" dest="$3" rec="${4:-}"
  if [ -d "$dest/.git" ]; then
    git -C "$dest" fetch --all --tags -q
  else
    # shellcheck disable=SC2086
    git clone $rec "$url" "$dest" || return 1
  fi
  git -C "$dest" checkout -q "$ref" || { warn "checkout $ref failed in $dest"; return 1; }
  [ -n "$rec" ] && git -C "$dest" submodule update --init --recursive -q
  return 0
}

mkvenv() { # dest pyX.Y
  local dest="$1" pyver="$2"
  if [ -x "$dest/bin/python" ] && [ -z "$FORCE" ]; then
    log "venv exists: $dest (use --force to rebuild)"; return 0
  fi
  [ -n "$FORCE" ] && rm -rf "$dest"
  local py; py="$(command -v "python$pyver" 2>/dev/null || true)"
  if [ -n "$py" ]; then uv venv --python "$py" "$dest"; else uv venv --python "$pyver" "$dest"; fi
}

vpip() { local venv="$1"; shift; uv pip install --python "$venv/bin/python" "$@"; }

dl() { # url dest-dir [outfile]
  local url="$1" dir="$2" name="${3:-}"
  mkdir -p "$dir"
  if [ -n "$name" ]; then wget -c -nv -O "$dir/$name" "$url"
  else wget -c -nv -P "$dir" "$url"; fi
}

pyok() { # venv import-check
  "$1/bin/python" -c "$2" >/dev/null 2>&1
}

# Old torch 1.12 (and some 2.0) wheels mark libtorch_*.so with an executable
# stack; this kernel refuses it, breaking `import torch`. Clear the flag.
clear_execstack() { # venv
  local venv="$1" sp
  sp=$(echo "$venv"/lib/python*/site-packages)
  "$venv/bin/python" /etc/bio/py/clear_execstack.py \
    "$sp/torch/lib/"*.so* "$sp/dgl/"**/*.so* "$sp/dgl/"*.so* 2>/dev/null || true
}

# CUDA-11 runtime libs (cusparse/curand/cudart) that dgl's cu11x wheels dlopen
# but the torch wheel doesn't bundle. Installed as pip wheels into the venv;
# the wrappers add site-packages/nvidia/*/lib to LD_LIBRARY_PATH.
install_cuda11_libs() { # venv
  vpip "$1" nvidia-cuda-runtime-cu11 nvidia-cusparse-cu11 nvidia-curand-cu11 \
    || warn "cu11 runtime libs failed to install (dgl may not import)"
}

# LD_LIBRARY_PATH additions so a venv's bundled torch + nvidia libs resolve.
venv_ld() { # venv -> echoes a path prefix
  local venv="$1" sp d out=""
  sp=$(echo "$venv"/lib/python*/site-packages)
  for d in "$sp"/torch/lib "$sp"/nvidia/*/lib; do [ -d "$d" ] && out="$d:$out"; done
  printf '%s' "$out"
}

# ------------------------------------------------------------------ ProteinMPNN
setup_proteinmpnn() {
  log "ProteinMPNN: clone @ $MPNN_REF"
  clone_pin "$MPNN_URL" "$MPNN_REF" "$SRC/proteinmpnn" || return 1
  mkvenv "$ENVS/proteinmpnn" 3.11 || return 1
  # repo ships no requirements.txt — install torch+numpy explicitly (verifier note)
  vpip "$ENVS/proteinmpnn" -r /etc/bio/requirements/proteinmpnn.txt || return 1
  pyok "$ENVS/proteinmpnn" "import torch,numpy" || { warn "proteinmpnn torch import failed"; return 1; }
  return 0
}

# ------------------------------------------------------------------------ ESM-2
setup_esm2() {
  log "ESM-2: venv + deps"
  mkvenv "$ENVS/esm2" 3.11 || return 1
  vpip "$ENVS/esm2" -r /etc/bio/requirements/esm2.txt || return 1
  clone_pin "$ESM_URL" "$ESM_REF" "$SRC/esm" || warn "esm repo clone failed (reference scripts only)"
  pyok "$ENVS/esm2" "import torch,transformers,esm" || { warn "esm2 imports failed"; return 1; }
  log "ESM-2: prefetch tiny (8M) + default ($BIO_ESM_DEFAULT_MODEL) weights"
  HF_HOME="$HF_HOME" "$ENVS/esm2/bin/python" - "$BIO_ESM_DEFAULT_MODEL" <<'PY' || warn "weight prefetch failed (will download on first use)"
import sys
from transformers import AutoTokenizer, AutoModelForMaskedLM
for m in dict.fromkeys(["facebook/esm2_t6_8M_UR50D", "facebook/" + sys.argv[1]]):
    print("prefetch", m)
    AutoTokenizer.from_pretrained(m)
    AutoModelForMaskedLM.from_pretrained(m)
PY
  return 0
}

# --------------------------------------------------------------------- EVOLVEpro
setup_evolvepro() {
  log "EVOLVEpro: clone @ $EVO_REF"
  clone_pin "$EVO_URL" "$EVO_REF" "$SRC/evolvepro" || return 1
  log "EVOLVEpro: core (CPU) venv"
  mkvenv "$ENVS/evolvepro-core" 3.11 || return 1
  vpip "$ENVS/evolvepro-core" -r /etc/bio/requirements/evolvepro-core.txt || return 1
  vpip "$ENVS/evolvepro-core" scikit-learn-extra==0.3.0 || warn "scikit-learn-extra optional (kmedoids) — skipped"
  vpip "$ENVS/evolvepro-core" --no-deps -e "$SRC/evolvepro" || warn "editable install of evolvepro (core) failed — CLI still works"
  log "EVOLVEpro: PLM (GPU) venv"
  mkvenv "$ENVS/evolvepro-plm" 3.11 || return 1
  vpip "$ENVS/evolvepro-plm" -r /etc/bio/requirements/evolvepro-plm.txt || return 1
  vpip "$ENVS/evolvepro-plm" --no-deps -e "$SRC/evolvepro" || warn "editable install of evolvepro (plm) failed — CLI still works"
  pyok "$ENVS/evolvepro-core" "import sklearn,pandas,xgboost" && pyok "$ENVS/evolvepro-plm" "import torch,esm" \
    || { warn "evolvepro imports failed"; return 1; }
  return 0
}

# ------------------------------------------------------------------- RFdiffusion
install_dgl() { # venv "space list of indexes" "space list of versions"
  local venv="$1" indexes="$2" versions="$3"
  # Check pip success + that libdgl landed — NOT `import dgl`, which also needs
  # the exec-stack fix and cu11 libs applied separately (chicken/egg otherwise).
  for idx in $indexes; do
    for v in $versions; do
      if uv pip install --python "$venv/bin/python" "dgl==$v" -f "$idx" >/dev/null 2>&1 \
         && find "$venv"/lib/python*/site-packages/dgl -name 'libdgl*.so*' 2>/dev/null | grep -q .; then
        log "dgl $v installed (from $idx)"; return 0
      fi
    done
  done
  return 1
}

setup_rfdiffusion() {
  local venv="$ENVS/rfdiffusion"
  log "RFdiffusion: clone @ $RFD_REF"
  clone_pin "$RFD_URL" "$RFD_REF" "$SRC/rfdiffusion" || return 1
  mkvenv "$venv" 3.10 || return 1
  vpip "$venv" -r /etc/bio/requirements/rfdiffusion.txt || return 1
  clear_execstack "$venv"   # torch 1.12 libtorch_cpu.so is marked RWE; kernel refuses it
  log "RFdiffusion: dgl 1.0.x (cu113)"
  install_dgl "$venv" "https://data.dgl.ai/wheels/cu113/repo.html" "1.0.2 1.0.1 1.0.0" \
    || warn "no dgl wheel installed for RFdiffusion"
  clear_execstack "$venv"
  install_cuda11_libs "$venv"   # dgl needs libcusparse.so.11 / libcurand.so.10 / libcudart.so.11.0
  vpip "$venv" "git+https://github.com/NVIDIA/dllogger.git" || warn "dllogger install failed"
  vpip "$venv" --no-deps "$SRC/rfdiffusion/env/SE3Transformer" || { warn "SE3Transformer install failed"; return 1; }
  vpip "$venv" --no-deps -e "$SRC/rfdiffusion" || { warn "rfdiffusion package install failed"; return 1; }
  log "RFdiffusion: weights -> $WTS/rfdiffusion"
  local base=http://files.ipd.uw.edu/pub/RFdiffusion
  dl "$base/6f5902ac237024bdd0c176cb93063dc4/Base_ckpt.pt"          "$WTS/rfdiffusion" Base_ckpt.pt
  dl "$base/e29311f6f1bf1af907f9ef9f44b8328b/Complex_base_ckpt.pt"  "$WTS/rfdiffusion" Complex_base_ckpt.pt
  dl "$base/60f09a193fb5e5ccdc4980417708dbab/Complex_Fold_base_ckpt.pt" "$WTS/rfdiffusion" Complex_Fold_base_ckpt.pt
  dl "$base/74f51cfb8b440f50d70878e05361d8f0/InpaintSeq_ckpt.pt"    "$WTS/rfdiffusion" InpaintSeq_ckpt.pt
  dl "$base/76d00716416567174cdb7ca96e208296/InpaintSeq_Fold_ckpt.pt" "$WTS/rfdiffusion" InpaintSeq_Fold_ckpt.pt
  dl "$base/5532d2e1f3a4738decd58b19d633b3c3/ActiveSite_ckpt.pt"    "$WTS/rfdiffusion" ActiveSite_ckpt.pt
  dl "$base/12fc204edeae5b57713c5ad7dcb97d39/Base_epoch8_ckpt.pt"   "$WTS/rfdiffusion" Base_epoch8_ckpt.pt
  LD_LIBRARY_PATH="$(venv_ld "$venv")$LD_LIBRARY_PATH" \
    pyok "$venv" "import torch,dgl,se3_transformer,rfdiffusion" \
    || { warn "rfdiffusion import check failed"; return 1; }
  return 0
}

# ---------------------------------------------------------- RoseTTAFold-All-Atom
setup_rfaa() {
  local venv="$ENVS/rfaa"
  log "RFAA: clone @ $RFAA_REF (with submodules)"
  clone_pin "$RFAA_URL" "$RFAA_REF" "$SRC/rfaa" --recurse-submodules || return 1
  mkvenv "$venv" 3.10 || return 1
  vpip "$venv" -r /etc/bio/requirements/rfaa.txt || return 1
  clear_execstack "$venv"
  log "RFAA: dgl (cu118) + torch-geometric ops"
  install_dgl "$venv" \
    "https://data.dgl.ai/wheels/cu118/repo.html https://data.dgl.ai/wheels/torch-2.0/cu118/repo.html" \
    "1.1.2 1.1.1 1.1.0" || warn "no dgl wheel installed for RFAA"
  clear_execstack "$venv"
  install_cuda11_libs "$venv"
  vpip "$venv" torch-scatter torch-sparse torch-cluster \
    -f https://data.pyg.org/whl/torch-2.0.1+cu118.html || warn "pyg ops install failed"
  vpip "$venv" torch-geometric==2.5.0 || warn "torch-geometric install failed"
  vpip "$venv" "git+https://github.com/NVIDIA/dllogger.git@0540a43971f4a8a16693a9de9de73c1072020769" || warn "dllogger failed"
  [ -d "$SRC/rfaa/rf2aa/SE3Transformer" ] && vpip "$venv" --no-deps "$SRC/rfaa/rf2aa/SE3Transformer"
  # rf2aa has no pyproject/setup.py; it runs as `python -m rf2aa.run_inference`
  # from the repo root (the wrapper cds there), so no editable install is needed.
  # Enable single-sequence / template-free inference without the MSA+template DBs:
  #  (1) a mappable-but-empty pdb100 template DB so FFindexDB init succeeds
  mkdir -p "$SRC/rfaa/pdb100_2021Mar03"
  : > "$SRC/rfaa/pdb100_2021Mar03/pdb100_2021Mar03_pdb.ffindex"
  printf '\0' > "$SRC/rfaa/pdb100_2021Mar03/pdb100_2021Mar03_pdb.ffdata"
  #  (2) patch load_protein to accept empty template files (-> blank_template)
  "$venv/bin/python" /etc/bio/py/rfaa_singleseq_patch.py "$SRC/rfaa/rf2aa/data/protein.py" \
    || warn "RFAA single-seq patch failed (template-free mode may not work)"
  log "RFAA: weights (RFAA_paper_weights.pt, ungated ~2GB)"
  dl "http://files.ipd.uw.edu/pub/RF-All-Atom/weights/RFAA_paper_weights.pt" "$SRC/rfaa" RFAA_paper_weights.pt
  warn "RFAA runs single-sequence only here: 399GB MSA DBs skipped; signalp6 is gated (signal-peptide path disabled)."
  LD_LIBRARY_PATH="$(venv_ld "$venv")$LD_LIBRARY_PATH" pyok "$venv" "import torch,dgl" \
    || { warn "rfaa torch/dgl import failed"; return 1; }
  return 0
}

# -------------------------------------------------------------------- AlphaFold 3
setup_alphafold3() {
  log "AlphaFold 3: clone @ $AF3_REF (code only; weights gated, DBs too large)"
  clone_pin "$AF3_URL" "$AF3_REF" "$SRC/alphafold3" || return 1
  mkdir -p "$WTS/alphafold3"
  log "AlphaFold 3: uv sync + build_data inside a Nix build shell (gcc/cmake/ninja/zlib)"
  # AF3 compiles a C++/pybind11 data pipeline; run the build inside nix-shell so
  # the compiler finds zlib headers. Best-effort — inference is blocked regardless.
  ( cd "$SRC/alphafold3" && \
    UV_PROJECT_ENVIRONMENT="$ENVS/alphafold3" \
    nix-shell -p uv git gcc cmake ninja zlib zstd hmmer python312 --run '
      set -e
      export UV_PROJECT_ENVIRONMENT="'"$ENVS/alphafold3"'"
      export UV_PYTHON_PREFERENCE=only-system
      uv sync
      uv run build_data
    ' ) || warn "AF3 uv sync / build_data failed (code is cloned; see README for the known blockers)"
  if [ -x "$ENVS/alphafold3/bin/python" ]; then
    pyok "$ENVS/alphafold3" "import jax; print(jax.devices())" || warn "AF3 jax import/devices check failed"
  fi
  warn "AlphaFold 3 inference is BLOCKED on this box: gated weights + ~630GB DBs + 8GB<16GB VRAM floor."
  return 0
}

# --------------------------------------------------------------------- dispatch
for t in "${TARGETS[@]}"; do
  case "$t" in
    proteinmpnn) setup_proteinmpnn ;;
    esm2)        setup_esm2 ;;
    evolvepro)   setup_evolvepro ;;
    rfdiffusion) setup_rfdiffusion ;;
    rfaa)        setup_rfaa ;;
    alphafold3)  setup_alphafold3 ;;
  esac
  if [ $? -eq 0 ]; then STATUS[$t]="ok"; else STATUS[$t]="FAILED"; fi
done

log "summary"
for t in "${TARGETS[@]}"; do printf '  %-14s %s\n' "$t" "${STATUS[$t]:-?}"; done
echo "bio-setup: full log at $LOG"
echo "bio-setup: run 'bio-doctor' to verify."
