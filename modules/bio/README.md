# modules/bio — protein-design / ML-biology toolkit

A reusable NixOS module that installs a suite of protein-design and protein-ML
tools behind uniform `bio-*` command-line wrappers, plus PyMOL for visualization.

For cloud runs, `bio-fold MODEL --fasta input.fasta --render` submits to the
configured head, downloads results to `~/bio-runs/JOB`, and renders locally.
`--view` opens PyMOL interactively. Folding models include Boltz-2, OpenFold3,
Protenix and RF3; the same interface also supports RFdiffusion, ProteinMPNN,
ESM-2 and EVOLVEpro. See [cloud setup and usage](../../machines/head/README.md).
RFAA remains available for explicit single-sequence runs while its separate full
database installation is parked. RF3 is installed on cloud workers through
`bio-fold rf3`; it does not require a workstation RF3 environment.

Use `bio-library` to store versioned proteins, oligos, small molecules and
assemblies on the head, then `bio-fold MODEL --construct NAME` or `--assembly NAME`.
The [construct library guide](../../machines/head/library/README.md) covers native
model adapters, modified chemistry and hourly verified local backups.

| Tool | Wrapper | What it does | On the 8GB 3070 |
|------|---------|--------------|-----------------|
| **ProteinMPNN** | `bio-mpnn` | fixed-backbone sequence design | ✅ runs great |
| **ESM-2** | `bio-esm` | protein LM: embeddings, logits, variant scoring | ✅ ≤650M (3B borderline, 15B no) |
| **EVOLVEpro** | `bio-evolvepro` | few-shot directed-evolution (PLM + top-model) | ✅ with ≤650M embeddings |
| **RFdiffusion** | `bio-rfdiffusion` | backbone generation / motif scaffolding / binders | ✅ up to a few hundred residues |
| **RoseTTAFold-All-Atom** | `bio-rfaa` | all-atom structure prediction | ⚠️ single-seq, small chains only |
| **AlphaFold 3** | `bio-af3` | structure prediction | ❌ blocked (see below) |
| **PyMOL** | `bio-viz` | visualize / render structures | ✅ |
| — | `bio-setup` | build the venvs + fetch weights | — |
| — | `bio-doctor` | diagnose the environment | — |

## How it works

- **`programs.nix-ld`** is enabled so ordinary PyPI/CUDA **manylinux wheels run on
  NixOS**. ProteinMPNN/ESM/EVOLVEpro/AF3 use *native* Nix Pythons (3.11/3.12), so
  their wheels import via the normal linker + `LD_LIBRARY_PATH` (the NVIDIA driver
  dir `/run/opengl-driver/lib` plus libstdc++/zlib from the Nix store). RFdiffusion
  and RFAA need Python 3.10 (not in this nixpkgs), so `uv` fetches a *managed* 3.10
  which runs via nix-ld.
- **`uv`** creates one virtualenv per tool under `bio.dataDir` from **pinned
  requirements** (`modules/bio/requirements/*.txt`) and **pinned git refs**
  (`modules/bio/scripts/bio-setup.sh`). The Nix store stays immutable; the venvs,
  cloned repos and model weights are mutable state under `bio.dataDir`.
- The wrappers, the pinned requirements, and helper Python CLIs are installed to
  the store + `/etc/bio` by the module. Machine-specific knobs live in
  `/etc/bio/config.sh`, generated from `options.bio.*`.

### Data layout (`bio.dataDir`, default `/opt/bio`)

```
/opt/bio/
  envs/         per-tool uv virtualenvs (proteinmpnn, esm2, evolvepro-core,
                evolvepro-plm, rfdiffusion, rfaa, alphafold3)
  src/          cloned upstream repos, pinned to exact commits
  weights/      downloaded model weights (rfdiffusion/, alphafold3/…)
  runs/         default output location for wrappers
  cache/        HF_HOME + TORCH_HOME (model download caches)
  logs/         bio-setup logs
```

## First-time setup

The module installs the commands; `bio-setup` downloads the heavy bits (run as the
`bio.user`, no sudo needed — it writes only under `bio.dataDir`):

```bash
bio-setup all              # or: bio-setup proteinmpnn esm2 evolvepro rfdiffusion
bio-doctor                 # verify GPU, envs, weights
```

Re-running is idempotent; `bio-setup <tool> --force` rebuilds one venv.

## Usage

### ESM-2 — `bio-esm`
```bash
bio-esm list-models
bio-esm embed  -i seqs.fasta -o emb.npz            # per-seq mean embeddings
bio-esm embed  -i seqs.fasta -o emb.npz --pooling per_tok
bio-esm logits -s MKTAYIAKQR -o logits.npz         # per-position AA logits
bio-esm score  -i seqs.fasta -o scores.csv         # unmasked per-residue log probabilities
bio-esm mutate -s MKTAYIAKQR -m A2G,K9R -o eff.csv # masked-marginal mutation effect
```
Model defaults to `bio.esm.defaultModel` (650M here). Override with `--model esm2_t36_3B_UR50D`.

### ProteinMPNN — `bio-mpnn`
```bash
bio-mpnn --pdb backbone.pdb --chains A --num-seqs 8 --temp 0.1 --out designs/
# designed sequences: designs/seqs/backbone.fa
bio-mpnn raw -- --pdb_path x.pdb --out_folder o --num_seq_per_target 16   # full native flags
```
Output is **sequence**, not structure — fold a design (ESMFold/AF) to get coordinates.

### EVOLVEpro — `bio-evolvepro`
```bash
# 1) embed WT + all candidate variants (GPU)
bio-evolvepro embed -i variants.fasta -o emb.csv
# 2) train on measured variants, propose the next round (CPU)
bio-evolvepro evolve -e emb.csv -l measured.csv -n 12 -o next_round.csv
```
`measured.csv` has columns `variant,activity`. With no `-l`, `evolve` proposes a
diverse first round (k-means over embeddings). The upstream repo is cloned at
`$BIO_DATA_DIR/src/evolvepro`; `bio-evolvepro repo -- <cmd>` runs inside it.

### RFdiffusion — `bio-rfdiffusion`
```bash
# unconditional monomer
bio-rfdiffusion --contigs '[150-150]' --num-designs 3 --out runs/mono
# motif scaffolding
bio-rfdiffusion --input-pdb motif.pdb --contigs '[10-40/A163-181/10-40]' --num-designs 3
# binder (uses Complex_base weights + hotspots)
bio-rfdiffusion --input-pdb target.pdb --contigs '[A1-150/0 70-100]' --hotspots '[A59,A83,A91]'
bio-rfdiffusion raw -- <hydra overrides>
```
Outputs backbone `.pdb` (+ `.trb`). Chain them: `bio-rfdiffusion` → `bio-mpnn` → fold → `bio-viz`.

### Visualize — `bio-viz`
```bash
bio-viz design.pdb                       # open interactively in PyMOL
bio-viz --render design.pdb -o img.png   # headless ray-traced PNG
bio-viz --overlay pred.pdb ref.pdb       # load + cealign onto the first
```

## Degraded / blocked tools

### RoseTTAFold-All-Atom (`bio-rfaa`) — works single-sequence; MSA is degraded
Code + Python env + network weights (`RFAA_paper_weights.pt`, ungated) install and
run on the 8 GB GPU. The full MSA/template pipeline needs ~399 GB of sequence DBs
(UniRef30/BFD) plus the ~81 GB pdb100 template DB — not installed here — and
**signalp6 is gated** (DTU EULA, not scriptable). So `bio-setup` sets RFAA up for
**single-sequence, template-free** inference:

```bash
bio-rfaa --fasta target.fasta --name job --out runs/rfaa   # -> runs/rfaa/job.pdb
```

To make this work without any DBs, `bio-setup` (a) drops a mappable-but-empty
pdb100 FFindex stub so the model initializes and (b) applies a one-line patch to
`load_protein` so empty template files mean "no templates" (blank template). The
wrapper auto-stages a query-only a3m so RFAA skips its DB search. This is genuinely
degraded vs. a real MSA — good for small single chains (<~500 aa), not for accuracy-
critical work. For MSA quality, generate an a3m elsewhere (e.g. the ColabFold MSA
server) and pass `--a3m msa.a3m`, or install the real DBs on a bigger machine.
Small-molecule/covalent inputs (which need openbabel + the template DB) are not
covered by the single-sequence path — use `bio-rfaa raw -- …` with real DBs.

### AlphaFold 3 (`bio-af3`) — blocked for inference
`bio-setup alphafold3` clones the code and builds its C++ data pipeline so imports
and the CPU data pipeline work. Real inference is **not possible on this machine**:
1. **Weights are gated** — request from Google, agree to the EULA; they may only be
   used if received directly from Google, so they can't be scripted. Drop the
   approved `af3.bin` into `/opt/bio/weights/alphafold3` to enable `bio-af3`.
2. **Databases** are ~630 GB decompressed — exceeds free disk.
3. **VRAM** — AF3's smallest documented GPU is a 16 GB V100; 8 GB is below the floor.

## Generalizing to another machine (e.g. a cloud GPU box)

The whole point of the module split: a new machine reuses `modules/bio` and only
overrides hardware knobs. Example `machines/cloud-gpu/configuration.nix`:

```nix
{ ... }:
{
  imports = [ ./hardware-configuration.nix ../../modules/bio ];

  bio = {
    enable = true;
    gpu.vramGB = 80;                          # H100/A100
    esm.defaultModel = "esm2_t48_15B_UR50D";  # the big model now fits
    dataDir = "/scratch/bio";                 # put state on fast local disk
  };
}
```
Add it to `flake.nix` (`mkSystem "cloud-gpu" "x86_64-linux" [] {}`), build, switch,
then `bio-setup all`. Same pinned recipes, bigger models. On such a box you can also
fetch the RFAA/AF3 databases and (with approved AF3 weights) run full pipelines.

To turn individual tools off on a machine: `bio.tools.alphafold3.enable = false;`.

## Options

| Option | Default | Meaning |
|--------|---------|---------|
| `bio.enable` | `false` | master switch |
| `bio.dataDir` | `/opt/bio` | mutable state dir (venvs, repos, weights) |
| `bio.user` / `bio.group` | `aydin` / `users` | owner of `dataDir` |
| `bio.gpu.vramGB` | `8` | informs safe defaults / warnings |
| `bio.esm.defaultModel` | `esm2_t33_650M_UR50D` | default ESM-2 checkpoint |
| `bio.torchCudaWheel` | `cu124` | PyTorch CUDA wheel tag |
| `bio.cudaCache` | `true` | add cuda-maintainers cachix substituter |
| `bio.tools.<name>.enable` | `true` | per-tool: pymol, esm, proteinmpnn, evolvepro, rfdiffusion, rfaa, alphafold3 |

## Troubleshooting

- `bio-doctor` — one-stop status (GPU, driver, nix-ld, per-tool envs/weights, a live `torch.cuda` check).
- **`torch.cuda.is_available()` is False** — check `/run/opengl-driver/lib/libcuda.so.1` exists and that `BIO_DRIVER_LIB` is on `LD_LIBRARY_PATH` (the wrappers set this).
- **A managed-Python tool won't start** (RFdiffusion/RFAA) — it needs nix-ld's env; the wrappers pull `NIX_LD*` from `/etc/set-environment`. Confirm `programs.nix-ld.enable = true` took effect (relogin after the first switch).
- **Rebuild after editing this module** — `sudo nixos-rebuild switch --flake ~/nixos-config#<machine>` (flakes only see git-tracked files, so `git add` new files first).
