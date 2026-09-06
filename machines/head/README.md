# Cloud bio toolkit

`bio-head` is a NixOS CPU server on DataCrunch/Verda. It provisions an Ubuntu/CUDA
GPU worker for each job, reuses model environments and weights on managed shared
storage, retrieves results, and deletes the worker. Jobs are currently serialized
so shared environment installation and volume attachment cannot race.

The workstation's `bio-fold` submits a job, downloads its results and optionally
renders a structure with PyMOL. This is temporary VM compute, not a serverless
HTTP endpoint or a SLURM cluster.

The [construct library](library/README.md) stores immutable molecular records on
the head and supplies model-specific inputs through `--construct` and `--assembly`.
Workstation backups retain and restore-check the complete library hourly.

## Run from the workstation

```bash
bio-fold boltz2 --seq NLYIQWLKDGGPSSGRPPPS --render
bio-fold openfold3 --fasta protein.fasta --render
bio-fold protenix --fasta protein.fasta --render
bio-fold rf3 --fasta protein.fasta --render
bio-fold rf3 --assembly enzyme-oligo --render
bio-fold rfdiffusion --contigs '[50-50]' --num 1 --render
bio-fold mpnn --pdb backbone.pdb --num 4
bio-fold esm --fasta proteins.fasta --sub score
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

RF3 uses the pinned official Foundry checkpoint and explicit MSAs for every
protein chain. Its defaults are 10 recycles, 50 diffusion steps and five samples;
`--num N` changes the sample count. See [RF3 setup and input support](rf3/README.md).
ESM and EVOLVEpro use the cached 650M ESM-2 checkpoint by default. Larger optional
ESM variants download on first use and need sufficient GPU memory.

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
Protenix 2.0.0 uses A100, L40S or H100 workers with CUDA 12.8. Its pinned
Torch/Triton kernels do not support Blackwell, and its compiled LayerNorm needs
the matching CUDA toolkit and Ninja. Unsupported explicit GPU choices are
rejected before renting a worker.

Boltz, Protenix, OpenFold3 and RF3 use the remote ColabFold MSA server by default.
That server searches large sequence databases and returns alignments;
we keep the model weights and query results locally. The private path described
below remains subject to full installation and quality comparison. RF3 shares
this ColabFold path. The separate RFAA database installation is parked; its
verified archives and completed data are retained for a later decision.
This is a difference in preprocessing and hosting, not evidence that OpenFold3
does not use databases. Reusing externally prepared RFAA alignments and template
coordinates would require a separately validated input path.
The public server's [usage notice](https://github.com/sokrypton/ColabFold/blob/main/colabfold/utils.py#L23-L31)
requires submissions from one IP and allows access limits for excessive use;
its documentation directs large workloads to local searches or a private server.
ColabFold's MIT software license is separate from hosted-service terms. A
commercial production deployment should establish its MSA service arrangements
and confidentiality requirements explicitly.

## Private MSA preparation

The separate 3000 GiB ColabFold volume has persistent retention. It holds the
complete classic CPU sequence databases, pairing taxonomy and template data.
`bio-msa install` provisions that snapshot on a transient high-memory worker;
installation is explicit and must finish before preparation can succeed.

```bash
# On the head, after complete database installation:
bio-msa prepare --model openfold3 --fasta protein.fasta --timeout 14400
bio-submit openfold3 --fasta protein.fasta --msa-backend private
# From the updated workstation wrapper:
bio-fold boltz2 --fasta protein.fasta --msa-backend private --render
bio-fold rf3 --assembly enzyme-oligo --msa-backend private --render
```

Private submissions first run preparation against a localhost-only MSA API.
The default selects available FIN-02 compute with at least 768 GiB RAM and an
instance price of at most $13/hour, including spot offers. Searches still use the
pinned CPU pipeline when the available host also has GPUs. `--worker TYPE`
overrides selection, and `--spot` restricts it to spot offers. The fresh launch
quote and total project budget are checked separately before allocation.
Submissions retain and validate the input bundle,
remove the preparation worker, then rent the prediction GPU. An explicit private
request fails if preparation is unavailable; it never switches to the public
server. Retained bundles bind their exact sequences, native inputs and database
provenance. The full RFAA HHsuite pipeline stays separate.
RF3 searches all distinct protein-chain sequences together, retains the raw
unpaired and paired responses, and binds each chain's A3M to its typed input.
Server-paired rows receive explicit synthetic RF3 pairing keys; these are
documented as pairing identifiers, not biological taxonomy annotations.

Public remains the default until comparisons establish suitable alignment,
pairing, template-feature and prediction quality. Miniature API tests and native
bundle replay tests establish compatibility only. Full database installation
and scientific comparisons are still pending. See [the implementation guide](msa/README.md)
and [storage contract](msa/STORAGE_CONTRACT.md).

Each submission sends a snapshot of the deployed recipes, helpers and
requirements over SSH into worker-local storage. The worker verifies its SHA-256
before executing it; `job.json` records the hash and the head retains the bundle.
This also avoids stale recipe content observed on the mutable shared filesystem.

Results are copied directly from each worker to head-local
`/var/lib/bio-runs/JOB/` before teardown. On this head/provider combination, a
persisted MSA manifest read as all NUL bytes over NFS 4.2 and read correctly after
a clean NFS 4.1 remount. All three head shares now use 4.1, and fresh worker mount
commands explicitly request it. This establishes the observed workaround for
those reads, not the server/kernel root cause or integrity of every database
file. Worker-direct result copying and worker-side database validation remain
in place; see [the retained evidence](VALIDATION.md#nfs-read-compatibility).
`run.log` and `job.json` record logs, instance identity, timeout and exit status.
Failed jobs preserve partial outputs when available and return a nonzero status.

## Full RoseTTAFold All-Atom databases

**Parked as of 2026-09-06.** The BFD extraction exceeded the managed filesystem's
observed 1 TiB per-file limit. Its verified archive and partial extraction remain;
the full-mode validation trigger is disabled. Storage is still retained and billed.
Resume requires resolving that storage limit and completing validation first.

RFAA defaults to full UniRef30/BFD MSA searches, PSIPRED secondary structure and
pdb100 template searches. `--sub single-seq` is an explicit database-free option;
failed full searches never silently become single-sequence predictions.
The cloud wrapper accepts a protein FASTA or a supported typed construct/library
assembly. Its explicit single-sequence path has passed mixed-input GPU checks;
those checks do not qualify full-database inference.

Full datasets expand to approximately 2.5 TiB. Use a **separate dedicated shared
volume** with at least 3 TiB capacity plus appropriate temporary-download
headroom, not the 100 GB model cache. Provisioning and retention are separate
from merely installing the model. Configure its ID and NFS export in
[`rfaa-storage.nix`](rfaa-storage.nix), then deploy. An empty configuration rejects
full RFAA submissions before renting a GPU. The 3300 GB allocation is now
registered with persistent retention. Its full installation is incomplete and paused.
Full-mode submission runs the fast database validator on the head before renting
a worker; the worker repeats validation against its own mount. Private MSA
preparation and serving also require a nonempty final installation receipt on the
head before rental. Merely downloading archives does not create that receipt.
Full-mode submissions require an active receipt and validated installed data.
See [the storage plan](rfaa/STORAGE_PLAN.md) for capacity,
retention costs, registration, and the allocation-specific cleanup procedure.

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

`rfaa-storage-expiry.timer` checks the registered policy every minute, catches
missed deadlines for timed receipts, and retries unfinished cleanup. Active
persistent receipts have no automatic expiry. Explicit retirement blocks queued full-mode jobs, stops
only recorded users of that allocation, and retires that volume while preserving
results on the original share and head. Provider or network failures can delay
teardown; the timer is not an absolute billing cutoff.

## Budget and cleanup

The configured ceiling is $500. `dc` estimates observed compute **and storage**
spending, imports the previous GPU ledger, reserves each job's maximum duration,
and requires a healthy watchdog before launching. The watchdog runs every minute
and deletes expired managed workers. Worker OS disks are included in confirmed
cleanup; shared databases and the head are protected.
The head waits three minutes after confirmed managed cleanup before another
launch, across submission controllers. `DC_LAUNCH_COOLDOWN_SECONDS` overrides
this delay; removal and watchdog actions never wait on it.

This is an estimated guard, not a provider-enforced billing cap. Protected
persistent storage continues billing after GPU work stops, so expensive databases
have an explicit retention policy. Both databases are retained persistently at
approximately $41.42/day combined; head and original storage bring the current
background to about $43.56/day before temporary compute. See [BUDGET.md](BUDGET.md) for formulas,
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
See [VALIDATION.md](VALIDATION.md) for executed checks, retained outputs and
remaining production-database validation.
