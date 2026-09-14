# Source on the preparation worker. Application code arrives in the verified job
# bundle; these immutable, version-checked executables are reusable on the share.
set -euo pipefail
[ "$(uname -sm)" = 'Linux x86_64' ] || { echo 'msa-tools: Linux x86_64 required' >&2; return 1; }
grep -qw avx2 /proc/cpuinfo || { echo 'msa-tools: this pinned build requires AVX2' >&2; return 1; }
if [ "${BIO_MSA_SEARCH_PROFILE:-}" = mapped-prefetch-128gb-v1 ]; then
  MSA_TOOLS_ROOT="${MSA_TOOLS_ROOT:-/mnt/bio-shared/envs/msa-tools-prefetch-v1}"
  # The head installs the frozen build once. Existing runtime packaging copies
  # this complete environment to workers; never build/download a replacement.
  python3 "$(dirname "${BASH_SOURCE[0]}")/native_runtime.py" verify --root "$MSA_TOOLS_ROOT" >/dev/null
  export MSA_TOOLS_ROOT
  export MMSEQS="$MSA_TOOLS_ROOT/bin/mmseqs" MMSEQS_SERVER="$MSA_TOOLS_ROOT/bin/mmseqs-server"
  export PATH="$MSA_TOOLS_ROOT/bin:$PATH"
  return 0 2>/dev/null || exit 0
fi
: "${MSA_TOOLS_ROOT:=/mnt/bio-shared/envs/msa-tools-v1}"
msa_mmseqs_commit=8cc5ce367b5638c4306c2d7cfc652dd099a4643f
msa_backend_commit=01365aa4735539ba95b417f73fb5326c77410394
mkdir -p "$(dirname "$MSA_TOOLS_ROOT")"
exec {msa_lock}>"$MSA_TOOLS_ROOT.lock"
flock "$msa_lock"
if [ ! -e "$MSA_TOOLS_ROOT" ]; then
  msa_stage="$MSA_TOOLS_ROOT.staging"
  mkdir -p "$msa_stage/bin"
  for msa_spec in \
    "mmseqs:$msa_mmseqs_commit:avx2:bd9b0234da5949ad528d5b5f9ea4cda9c1e23dce14b46c0791d4d919a76e61ce" \
    "mmseqs-server:$msa_backend_commit:x86_64:0951d6a880c98d350140cacd56f755f03e53c63299317193c8b4f723af82b5bf"; do
    IFS=: read -r msa_name msa_commit msa_arch msa_sha <<< "$msa_spec"
    msa_archive="$msa_stage/$msa_name.tar.gz"
    if ! printf '%s  %s\n' "$msa_sha" "$msa_archive" | sha256sum -c --status 2>/dev/null; then
      curl --fail --location --silent --show-error --retry 5 --connect-timeout 30 --output "$msa_archive.part" \
        "https://mmseqs.com/archive/$msa_commit/$msa_name-linux-$msa_arch.tar.gz"
      printf '%s  %s\n' "$msa_sha" "$msa_archive.part" | sha256sum -c -
      mv "$msa_archive.part" "$msa_archive"
    fi
    tar -xzf "$msa_archive" -C "$msa_stage" --no-same-owner "$msa_name/bin/$msa_name"
    if [ "$msa_name" = mmseqs ]; then
      tar -xzf "$msa_archive" -C "$msa_stage" --no-same-owner "$msa_name/LICENSE.md"
    else
      tar -xzf "$msa_archive" -C "$msa_stage" --no-same-owner "$msa_name/LICENSE"
    fi
    install -m 755 "$msa_stage/$msa_name/bin/$msa_name" "$msa_stage/bin/$msa_name"
  done
  [ "$("$msa_stage/bin/mmseqs" version)" = "$msa_mmseqs_commit" ]
  [ "$("$msa_stage/bin/mmseqs-server" -version)" = "$msa_backend_commit" ]
  (cd "$msa_stage"; sha256sum bin/mmseqs bin/mmseqs-server > executables.sha256)
  printf '%s\n%s\n' "$msa_mmseqs_commit" "$msa_backend_commit" > "$msa_stage/versions.txt"
  mv "$msa_stage" "$MSA_TOOLS_ROOT"
fi
(cd "$MSA_TOOLS_ROOT"; sha256sum -c executables.sha256)
[ "$("$MSA_TOOLS_ROOT/bin/mmseqs" version)" = "$msa_mmseqs_commit" ]
[ "$("$MSA_TOOLS_ROOT/bin/mmseqs-server" -version)" = "$msa_backend_commit" ]
flock -u "$msa_lock"
exec {msa_lock}>&-
export MSA_TOOLS_ROOT
export MMSEQS="$MSA_TOOLS_ROOT/bin/mmseqs"
export MMSEQS_SERVER="$MSA_TOOLS_ROOT/bin/mmseqs-server"
export PATH="$MSA_TOOLS_ROOT/bin:$PATH"
