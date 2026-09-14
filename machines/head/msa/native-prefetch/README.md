# Pinned mapped-prefetch native runtime

`mapped-prefetch-128gb-v1` explicitly selects the separate environment
`envs/msa-tools-prefetch-v1`. Both `resident-768gib-v1` and `mapped-128gb-v1`
continue to select the original upstream `envs/msa-tools-v1`. The profile has
the same complete databases, index format, backend scripts, search parameters
and ordered scoring loop. Its native build adds bounded parallel reads before
the existing posting-list copies and scoring.

The installed MMseqs ELF is the exact tested `gc-reader-sweep-v2` artifact,
SHA-256 `d33f1b17a8dff3060025c9aa78ed93a184719d7c63188506b3254d438c3df8a5`.
`../native-runtime.json` separately pins the recursive launcher, ELF, dynamic
loader, every shared library and unchanged MMseqs API backend. The launcher
uses `ld-linux --argv0` so recursively generated MMseqs workflows use the same
portable runtime. Linux `/proc/PID/cmdline` still exposes the loader arguments;
process identification must account for that.

## Execution bounds

The native feature defaults off. Only the explicit profile sets
`GC_MMSEQS_POSTING_PREFETCH=1`, `GC_MMSEQS_POSTING_RANDOM=1` and
`GC_MMSEQS_POSTING_READERS=32`. Switching profiles removes those variables.
Reader count parsing accepts only 4, 16 or 32; the unconfigured experimental
default remains 4. A window contains at most 128 similar-kmer IDs and at most
64 MiB of touched-page references. That is not a cap on physical I/O or resident
memory. The managed profile separately requires a 96 GiB cgroup and zero swap.

Each QueryMatcher owns its reader pool. One query observed 32 helper threads
plus four native threads; four simultaneously active queries can create 128
helpers plus native threads. Original copy/scoring order remains unchanged
after a window completes. RANDOM advice applies only to complete pages inside
validated read-only file mappings; skipped ranges are logged. Sequence scoring,
expansion and export are still unmodified and can remain I/O bound.

## Source and build record

`mmseqs.patch` is the complete diff from upstream commit
`8cc5ce367b5638c4306c2d7cfc652dd099a4643f`. It changes only QueryMatcher and adds
PostingPrefetch.h. `build-provenance.json` pins the upstream archive, every changed
source file, Nix toolchain, portable library origins and actual incremental
compile/link commands. The tested build reused the unchanged framework objects
from the preceding complete build and recompiled QueryMatcher and Version.

To verify source preparation without compiling:

```sh
python3 rebuild.py --archive /path/to/MMseqs2-8cc5ce367b5638c4306c2d7cfc652dd099a4643f.tar.gz \
  --output /new/empty-build-directory --prepare-only
```

Omit `--prepare-only` for a clean build using the recorded Nix toolchain. The
output directory must not already exist. A clean independent build has not
been shown to reproduce the identical ELF, so the script records its digest
and never installs it. A different ELF requires a new reviewed lock and new
qualification; it cannot silently replace the tested artifact.

The upstream source archive retains MMseqs licensing and bundled dependency
sources. The library origins in the build record identify the exact GCC and
glibc distributions used. Keep these source/build records with the internally
packaged runtime when transferring it.

## Installation and runtime packaging

From the MSA tools directory:

```sh
python3 native_runtime.py install --source /path/to/frozen-assets \
  --root /mnt/bio-shared/envs/msa-tools-prefetch-v1
python3 native_runtime.py verify --root /mnt/bio-shared/envs/msa-tools-prefetch-v1
```

The source directory supplies the exact `bin/` and `portable/` members listed
in the lock. Installation rejects redirected paths, verifies every member,
stages a new directory, checks both real versions, and atomically promotes it.
An existing destination is verified and never overwritten. `tools.sh` verifies
this installation before use and does not download or build a fallback.

The existing worker-runtime archive mechanism packages this environment when
given `--search-profile mapped-prefetch-128gb-v1`. New runtime plans record that
profile; old plans and old runtime selections retain their previous shape.
Installation alone does not activate this profile or change head defaults.

The retained toy and recursive-workflow receipts establish exact local output
parity and helper bounds for their fixtures. Full editor and complete-database
quality qualification is separate and must be established by a completed run;
a timed-out diagnostic is not a passing qualification receipt.

The exact pinned artifact subsequently passed the September 14 full-database
1,726-residue editor comparison and a fresh short88 managed-session request.
Both preserved the retained reference outputs and completed within the 96 GiB
cap without OOM or swap, followed by exact worker/OS/cache-lease cleanup. See
[measured timings and comparison limits](../README.md). These results qualify
this artifact and profile for the tested workloads; a different build still
requires its own qualification.
