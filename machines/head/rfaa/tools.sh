# Source this on Ubuntu/CUDA workers to install/reuse the CPU preprocessing tools.
# Conda is isolated from RFAA's torch environment; all tools live on shared NFS.
set -euo pipefail
: "${SHARED:=/mnt/bio-shared}"
: "${RFAA_TOOLS_ROOT:=$SHARED/envs/rfaa-tools-v1}"
RFAA_MAMBA="$SHARED/bootstrap/micromamba-2.5.0/bin/micromamba"
if [ ! -x "$RFAA_MAMBA" ]; then
  mkdir -p "$(dirname "$RFAA_MAMBA")"
  archive="$SHARED/bootstrap/micromamba-2.5.0.tar.bz2"
  curl --fail --location --retry 5 --connect-timeout 30 --output "$archive.part" \
    https://conda.anaconda.org/conda-forge/linux-64/micromamba-2.5.0-1.tar.bz2
  printf '%s  %s\n' 4ae6e5cdff233616c94d4bb69cf77a572d67b0b227073de12c3aa0ff23795ded "$archive.part" | sha256sum -c -
  mv "$archive.part" "$archive"
  tar xjf "$archive" -C "$SHARED/bootstrap/micromamba-2.5.0" bin/micromamba
fi
export MAMBA_ROOT_PREFIX="$SHARED/cache/mamba"
if [ ! -f "$RFAA_TOOLS_ROOT/.rfaa-tools-v1" ]; then
  # 4.01 is supplied by biocore, matching upstream environment.yaml. Bioconda's
  # distinct psipred=4.0 package has a different on-disk layout.
  "$RFAA_MAMBA" create --yes --prefix "$RFAA_TOOLS_ROOT" --override-channels \
    --strict-channel-priority -c conda-forge -c biocore -c bioconda \
    python=3.10 hhsuite=3.3.0 csblast=2.2.3 blast-legacy=2.2.26 biocore::psipred=4.01
  "$RFAA_MAMBA" list --prefix "$RFAA_TOOLS_ROOT" --explicit > "$RFAA_TOOLS_ROOT/packages-explicit.txt"
  touch "$RFAA_TOOLS_ROOT/.rfaa-tools-v1"
fi
export PATH="$RFAA_TOOLS_ROOT/bin:$PATH"
export PSIPRED_DATA="$RFAA_TOOLS_ROOT/share/psipred_4.01/data"
export CSBLAST_DATA="$RFAA_TOOLS_ROOT/data"
export BLASTMAT="$RFAA_TOOLS_ROOT/share/blast-2.2.26/data"
# Legacy BLAST package layouts differ across builds; locate its scoring matrix.
if [ ! -s "$BLASTMAT/BLOSUM62" ]; then
  blast_matrix=$(find "$RFAA_TOOLS_ROOT" -type f -name BLOSUM62 -print -quit)
  [ -n "$blast_matrix" ] || { echo 'rfaa: BLAST scoring matrices missing' >&2; return 1; }
  export BLASTMAT="$(dirname "$blast_matrix")"
fi
for executable in hhblits hhfilter hhsearch csbuild makemat psipred psipass2; do
  command -v "$executable" >/dev/null || { echo "rfaa: missing preprocessing tool $executable" >&2; return 1; }
done
for resource in "$PSIPRED_DATA/weights.dat" "$PSIPRED_DATA/weights.dat2" \
  "$PSIPRED_DATA/weights.dat3" "$PSIPRED_DATA/weights_p2.dat" "$CSBLAST_DATA/K4000.crf"; do
  [ -s "$resource" ] || { echo "rfaa: missing preprocessing resource $resource" >&2; return 1; }
done
