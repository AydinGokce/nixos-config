# Cloud MD runtime

This directory builds a pinned Linux x86_64 environment once and restores its
verified archive on ephemeral workers. Package solving, source checkout and
compilation are absent from the worker restore path. No credentials are needed
to obtain these public software packages.

The CPU and CUDA variants use GROMACS **2026.3**, PLUMED **2.10.1**, Python
**3.11**, NumPy **1.26.4**, SciPy **1.12.0**, alchemlyb **2.5.0**, pyMBAR
**4.0.3**, MDAnalysis **2.9.0**, ParmEd **4.3.0**, and RDKit **2025.03.6**.
Every conda package URL, build, SHA-256, size and declared license is in the
variant's `linux-64-*.lock.json`. Official source archives and the alchemlyb
release wheel are pinned separately in `pins.json`.

The pmx source is the Python 3 **develop** branch at
[`0dd5f0a9cdf26109eff98bdfeb4ac4e55353aa76`](https://github.com/deGrootLab/pmx/tree/0dd5f0a9cdf26109eff98bdfeb4ac4e55353aa76).
Its package/module version records that revision as `0+g0dd5f0a9cdf2`. Its master
branch is Python 2 and is not used. A packaging-only patch replaces its obsolete
setuptools bootstrap requirement and supplies version metadata for the pinned
archive; the scientific algorithms and hybrid force-field files are unchanged.

## BFEE3 identity and scope

The actual upstream is [fhh2626/BFEE3](https://github.com/fhh2626/BFEE3).
Its current **3.2.1** release deliberately retains distribution name `bfee2`,
module `BFEE2`, and GUI command `BFEE2Gui.py`. This runtime builds upstream commit
[`8b3e33e39b74bf1a7b58167df92af7f5f5795180`](https://github.com/fhh2626/BFEE3/tree/8b3e33e39b74bf1a7b58167df92af7f5f5795180),
whose README explicitly identifies BFEE3. It does not substitute an older BFEE2
release or use the `bfee3` alias package as an implementation.

The supported headless entry points include:

```python
from BFEE2.inputGenerator import inputGenerator
from BFEE2.postTreatment import postTreatment

inputGenerator().generateGromacsGeometricFiles(
    path, topFile, pdbFile, pdbFileFormat,
    ligandOnlyTopFile, ligandOnlyPdbFile, ligandOnlyPdbFileFormat,
    selectionPro, selectionLig,
    selectionSol="resname TIP3* or resname SPC* or resname HOH or resname WAT or resname SOL",
    temperature=300.0,
)
```

BFEE3's upstream GROMACS integration implements the **geometric route** using
Colvars. Its NAMD alchemical/LDDM/WTM-λABF routes are separate capabilities and
NAMD is not included here. The pmx/GROMACS mutation pipeline is a distinct
alchemical method. BFEE3's optional AI assistant is not used by this runtime.

## Build once

Run on Linux x86_64 with a working glibc ELF loader and Python 3.11.8 or newer
(Ubuntu 24.04 provides Python 3.12). NixOS also needs `nix-ld` for the conda ELF
binaries. The installer handles GROMACS's shell dispatcher and discovers the
NixOS NVIDIA driver library directory when present.

```sh
./install.sh --variant cpu --prefix /var/lib/bio-md/build-cpu \
  --cache /var/cache/bio-md-build \
  --pack /mnt/bio-shared/md-runtime/bio-md-cpu-linux-64.tar.gz

./install.sh --variant cuda --prefix /opt/bio-md-build-cuda \
  --cache /var/cache/bio-md-build --gpu-smoke \
  --pack /mnt/bio-shared/md-runtime/bio-md-cuda-linux-64.tar.gz
```

Downloads are approximately **700 MB CPU** and **2.9 GB CUDA**, plus the pinned
source archives; installed/build-cache space is larger. `--download-only` fills
a verified local cache without constructing an environment. The compiler used
for extension builds, sysroot, build tools and complete Python dependency
closure are locked too. The builder's initial pmx check currently expects `cpp`
on its PATH; restored runtimes automatically select their pinned preprocessor.
The installer builds only the small pmx native
extensions and the BFEE3 wheel, not GROMACS or PLUMED. Reuse built archives for
jobs rather than sharing a package-build cache between workers.

`--gpu-smoke` requires an attached NVIDIA GPU. CUDA packages can be provisioned
without one, but that does not count as GPU qualification. This CUDA variant
contains CUDA **12.9**; validate the actual cloud driver's compatibility with
`--smoke --gpu` before production use. The local qualification uses an RTX 3070
with driver 595.71.05. A container's toolkit version alone does not establish
the host driver's compatibility.

The output prefix contains:

- `bin/{gmx,plumed,pmx,python}` and the complete scientific Python environment;
- `activate.sh`, which sets `PATH`, `GMXLIB`, `PLUMED_KERNEL` and library paths;
- `manifest.json` with runtime fingerprint, source revisions, wheel hashes,
  complete package inventory, engine versions and smoke receipts;
- `share/bio-md-runtime/` with source pins, lock, source licenses and smoke tool;
- `smoke-cpu/report.json`, and `smoke-gpu/report.json` when requested.

The fingerprint covers both dependency/source pins and the installer, packer
and smoke source. Build artifacts additionally have their own SHA-256 receipts. This
pins the dependency graph and identifies the precise published archive; it does
not claim that arbitrary builders produce byte-identical compiler output.

The packer handles overlapping CUDA development-package paths explicitly. It
selects the cached owner whose bytes, after normal conda prefix relocation,
exactly match the qualified installed file. It aborts if no owner matches;
extraction never relies on the ordering of duplicate tar members. Each archive
has a `.packing.json` receipt listing these paths and their installed hashes.

## Restore on a worker

Publish each archive and its `.sha256` sidecar under the shared MD runtime
cache. Publish `current.json` with top-level
`"schema": "bio-md-runtime-deployment.v1"` and `cpu`/`cuda` entries containing
`archive`, `sha256`, and `fingerprint`. Keep the runtime source checkout
and that release metadata together.

```sh
python3 restore.py --archive /mnt/bio-shared/md-runtime/bio-md-cuda-linux-64.tar.gz \
  --sha256 "$EXPECTED_ARCHIVE_SHA256" --fingerprint "$EXPECTED_RUNTIME_FINGERPRINT" \
  --variant cuda --prefix /opt/bio-md --smoke --gpu
source /opt/bio-md/activate.sh
gmx --version
```

`--variant` requires the fingerprint computed from this checkout's committed
pins and installer; it does not trust a substituted cache index. Extraction
checks the archive hash, rejects duplicate members, escaping paths/links and special files,
requires a qualified manifest, restores into an owned temporary directory,
then runs `conda-unpack` at its final prefix. Locks are local to that worker's
destination directory. An existing matching restored prefix can be reused;
another environment is never overwritten. This path has no network calls,
package resolution, or compilation.

Restore also repairs GROMACS's shell dispatcher for NixOS and creates the
relative `bin/cpp` alias needed by pmx, pointing at the bundled
`x86_64-conda-linux-gnu-cpp`. PMX topology preprocessing therefore uses the
pinned compiler without requiring a host GCC installation. Standard Linux
shell utilities (`bash`, `cat`, `grep`, `uname`) remain host prerequisites.

## Engine support and validation

The installed GROMACS build reports both Colvars and PLUMED enabled. Each build
must run actual `grompp`/`mdrun` smoke calculations that deposit PLUMED WTMetaD
hills, execute a Colvars harmonic restraint, and export all λ-state energies
that alchemlyb parses. The smoke also runs a pmx mutation and hybrid-topology
generation, imports the exact BFEE3 API and RDKit, and estimates a known-zero
free-energy difference with pyMBAR. GPU qualification explicitly requires
nonbonded GPU offload. These short fixtures verify software interfaces; they
do not establish biomolecular accuracy or sampling convergence.

The [bundled GROMACS PLUMED interface](https://manual.gromacs.org/2026.3/reference-manual/special/plumed.html)
supports this WTMetaD/umbrella use. It requires **`-ntmpi 1`** with this thread-MPI
build. PLUMED replica exchange, ENERGY CV coupling, and λ dynamics are not
supported by that interface and must not be silently treated as enabled.
Colvars is a separate engine interface used by BFEE3.

Protein, DNA and RNA simulations require complete compatible topologies.
Synthetic amidites or other nonstandard residues require their exact validated
chemical graphs and force-field parameters from the higher-level chemistry
admission layer. This runtime does not infer missing parameters or replace
nonstandard chemistry with canonical residues. Force-field family, water model,
parameter hashes and sampling protocol must accompany scientific results.

## Licenses and maintenance

GROMACS is LGPL-2.1-or-later; PLUMED and pmx use LGPL licenses; BFEE3 is GPLv3.
alchemlyb and RDKit use BSD licenses, and pyMBAR uses MIT. See the pinned source
licenses and each exact package's license field in the lock. CUDA components
retain NVIDIA's individual license terms. No NAMD software or license is
bundled. Preserve these notices when redistributing runtime archives.

To update versions, resolve a new explicit dependency closure, retain package
URLs and SHA-256s in both locks, update upstream source/wheel pins deliberately,
then rebuild and requalify both variants plus the actual target cloud GPU.
Production installation never solves against mutable channel metadata.

Run the bootstrap/archive tests with:

```sh
python3 -m unittest discover -s . -p 'test_*.py' -v
```
