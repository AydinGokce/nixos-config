# Cloud bio toolkit

`bio-head` is a NixOS CPU server on DataCrunch/Verda. It provisions an Ubuntu/CUDA
GPU worker for each job, reuses model environments and weights on managed shared
storage, retrieves results, and deletes the worker. Jobs are currently serialized
so shared environment installation and volume attachment cannot race.

The workstation's `bio-fold` submits a job, downloads its results and optionally
renders a structure with PyMOL. This is temporary VM compute, not a serverless
HTTP endpoint or a SLURM cluster.

## Run from the workstation

```bash
bio-fold boltz2 --seq NLYIQWLKDGGPSSGRPPPS --render
bio-fold openfold3 --fasta protein.fasta --render
bio-fold protenix --fasta protein.fasta --render
bio-fold rfaa --fasta protein.fasta --render
bio-fold rfaa --fasta protein.fasta --sub single-seq --render
bio-fold rfdiffusion --contigs '[50-50]' --num 1 --render
bio-fold mpnn --pdb backbone.pdb --num 4
bio-fold esm --fasta proteins.fasta --sub score --model esm2_t6_8M_UR50D
bio-fold evolvepro --fasta variants.fasta --labels measured.csv --num 4
```

`--view` opens PyMOL; `--render` writes `render.png`. Results default to
`~/bio-runs/JOB/`; use `--out DIR` to override. Model arguments follow `--`.
`--timeout SECONDS` limits a run (default two hours). `--gpu TYPE` requests a
specific instance; omit it for compatibility-aware capacity fallback. The suffix
in `1A100.22V` denotes CPU allocation: that instance has an **80 GB A100**.

EVOLVEpro accepts CSV columns `variant,activity`, ranks unmeasured variants and
writes selected sequences. See [EVOLVEPRO.md](EVOLVEPRO.md) for first-round
selection and embedding-only runs.

## Head and storage

```bash
ssh -i ~/.ssh/datacrunch_ed25519 root@31.56.109.100
# Tailscale is already enrolled; use your tailnet's bio-head address if desired.
dc ls
dc spend
```

The head has a 50 GB OS disk. `/mnt/bio-shared` is the original 100 GB managed
shared filesystem in FIN-02, containing `envs/`, `src/`, `weights/`, `cache/`,
`tools/` and job staging directories. CPU/GPU workers must launch in FIN-02 to
attach it. Old CUDA environments use Ampere-compatible GPU choices; the RTX
A6000 requires the CUDA 12.6 image. Modern recipes can use newer GPUs.

Results are copied directly from each worker to head-local
`/var/lib/bio-runs/JOB/` before teardown. This avoids an observed NFS issue where
the NixOS head reads worker-written files as zero-filled. `run.log` and `job.json`
record logs, instance identity, timeout and exit status. Failed jobs preserve
partial outputs when available and return a nonzero status.

## Full RoseTTAFold All-Atom databases

RFAA defaults to full UniRef30/BFD MSA searches, PSIPRED secondary structure and
pdb100 template searches. `--sub single-seq` is an explicit database-free option;
failed full searches never silently become single-sequence predictions.

Full datasets expand to approximately 2.5 TiB. Use a **separate dedicated shared
volume** with at least 3 TiB capacity plus appropriate temporary-download
headroom, not the 100 GB model cache. Provisioning and retention are separate
from merely installing the model. Configure its ID and NFS export in
[`rfaa-storage.nix`](rfaa-storage.nix), then deploy. An empty configuration rejects
full RFAA submissions before renting a GPU.

```bash
bio-rfaa-databases plan
bio-rfaa-databases install
bio-rfaa-databases validate
```

The downloader runs on the head against `/mnt/bio-databases/rfaa`; it resumes
archive downloads and records validated installation receipts. RFAA workers
attach that volume read-only. Keep results and model caches outside this
replaceable database volume. See [the database/tooling guide](rfaa/README.md)
for sources, integrity checks, external database adoption and licensing notes.
SignalP trimming is optional and omitted; core MSA/template inference does not
require a SignalP credential.

## Budget and cleanup

The configured ceiling is $500. `dc` estimates observed compute **and storage**
spending, imports the previous GPU ledger, reserves each job's maximum duration,
and requires a healthy watchdog before launching. The watchdog runs every minute
and deletes expired managed workers. Worker OS disks are included in confirmed
cleanup; shared databases and the head are protected.

This is an estimated guard, not a provider-enforced billing cap. Protected
persistent storage continues billing after GPU work stops, so expensive databases
need an explicit retention policy. See [BUDGET.md](BUDGET.md) for formulas,
limitations and recovery commands. Automatic account top-ups do not reset spending.

```bash
dc types --gpu
dc run 1A100.22V --max-hours 1 -- nvidia-smi
dc rm INSTANCE_ID                 # only a managed temporary worker
dc watchdog
systemctl status dc-budget-watchdog.timer
```

API credentials are read from `/root/.config/datacrunch/credentials.env`, and the
worker SSH key from `/root/.ssh/datacrunch_ed25519`. Both remain outside Git with
restricted permissions. No new API key is required for the current model setup.

## Deploy and validate

```bash
~/nixos-config/machines/head/deploy.sh
```

The helper builds and activates the `head` flake on the existing server. It takes
an optional host argument and `DC_SSH_KEY` override. Workstation changes are part
of `nixosConfigurations.workstation-x86_64`; activate with the usual NixOS rebuild.

Offline checks (use an available Python interpreter):

```bash
python3 -m unittest discover -s machines/head -p 'test_*.py'
python3 -m unittest discover -s machines/head/rfaa -p 'test_*.py'
python3 -m unittest discover -s modules/bio/tests -p 'test_bio_fold.py'
```

EVOLVEpro's numerical tests also require pandas, numpy and scikit-learn; the
existing local `evolvepro-core` environment supplies them. Nix builds validate the
head and workstation configurations. A package install or successful Nix build
alone does not establish successful model inference; check real artifacts and
record each cloud test separately. AF3 is outside the current validation scope.
