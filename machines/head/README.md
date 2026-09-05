# bio-head — DataCrunch orchestrator node

An always-on, cheap DataCrunch CPU instance running NixOS that launches
**ephemeral GPU instances per job** and tears them down — the consolidated,
budget-guarded control plane for heavy GPU runs.

- **Provider/instance:** DataCrunch `CPU.4V.16G` (~$0.048/hr ≈ $35/mo), FIN-02.
- **Current IP:** `31.56.109.100` (DataCrunch static for the instance's life).
- **OS:** NixOS 26.05, installed onto a stock Ubuntu instance with `nixos-anywhere`
  (kexec confirmed working on DataCrunch's KVM hypervisor; legacy-BIOS GRUB).

## Access

```bash
ssh -i ~/.ssh/datacrunch_ed25519 root@31.56.109.100      # automation key
ssh root@31.56.109.100                                    # your ~/.ssh/id_ed25519 also authorized
```
Tailscale is installed but not yet joined — run once (needs your key):
`ssh root@… 'tailscale up --auth-key=tskey-…'`, then reach it by tailnet name.

## The `dc` CLI (on the head)

```bash
dc types --gpu                 # list GPU instance types + on-demand/spot prices
dc ls                          # your running instances
dc launch 1H100.80S.30V        # provision (CUDA+Docker Ubuntu image); prints id + ip
dc launch 1A100.22V --spot     # spot pricing (cheaper, preemptible)
dc ssh <id|name> -- nvidia-smi # ssh into a GPU node (root, automation key)
dc run 1H100.80S.30V -- <cmd>  # launch, run cmd, then ALWAYS destroy (trap) — safest
dc rm <id|name|all>            # destroy instance(s)
dc spend                       # estimated cumulative spend vs the $500 ceiling
```

Budget guard: `dc launch` refuses once estimated spend ≥ `DC_BUDGET_CEILING`
(default **$500**). Spend is estimated from a local ledger
(`/var/lib/dc/ledger.tsv`: id, type, $/hr, start, end) since DataCrunch auto-top-up
hides the real balance. **Always `dc rm` a GPU node when done** — a forgotten box
is the only real way to burn budget.

## Running a heavy job (the ephemeral pattern)

Ephemeral GPU nodes stay **stock Ubuntu + CUDA + Docker** (no nixos-anywhere per
job — that would add minutes each time). Reproducibility lives here on the NixOS
head and in your **container images**. So a job is:

```bash
id=$(dc launch 1H100.80S.30V --name af3 | grep -oE 'id=[0-9a-f-]+' | cut -d= -f2)
dc ssh "$id" -- docker run --gpus all -v /data:/data <your-model-image> <args>
dc ssh "$id" -- 'cat /data/out/*'        # or rsync results back to the head/NFS
dc rm "$id"
```

…but for the built-in models, use **`bio-submit`** (below), which does all of this.

## Shared NFS (`/mnt/bio-shared`)

A DataCrunch `NVMe_Shared` volume (`bio-shared`, 100 GB, FIN-02, ~$0.20/GB/mo =
$20/mo) mounted at `/mnt/bio-shared` on the head and on every ephemeral GPU node.
Holds per-tool venvs, cloned repos, model weights, caches, and run outputs, so
they're built/downloaded **once** and reused across jobs. Structure:
`envs/ src/ weights/ dbs/ runs/ cache/`.

- Truly shared: a venv/weight written by one node is visible to the next.
- **Region-locked** to FIN-02 — `bio-submit` therefore launches GPU nodes in
  FIN-02 (its `--loc`). If FIN-02 lacks capacity for a GPU, the job fails fast;
  retry or pick another type.
- Nodes mount it with `nolock` (their `rpc.statd` is masked) after the volume is
  attached at launch (`dc launch --volume <id>`).
- **Scaling for AF3/RFAA databases:** the ~630 GB AF3 + ~399 GB RFAA DB set would
  need ~1.5 TB ≈ **$300/mo** — a deliberate cost decision. Resize the volume (or
  make a bigger one) before enabling those; the 100 GB share here is sized for the
  no-DB models' envs/weights/outputs.

## `bio-submit` — run a model on an ephemeral GPU

Launches a GPU node (falling back across GPU types when FIN-02 capacity is tight),
waits for sshd, runs a per-tool recipe (`recipes/<tool>.sh`) that builds the env +
weights on the shared FS (once, cached), folds/designs, then **rsyncs the results
to head-local `/var/lib/bio-runs/<jobid>/` and always destroys the node**.

```bash
# Folding (no local DBs — remote ColabFold MSA server or single-seq):
bio-submit boltz2    --fasta prot.fasta        # MIT, AF3-class (+affinity) — VALIDATED
bio-submit protenix  --fasta prot.fasta        # ByteDance AF3 repro (Apache-2)
bio-submit openfold3 --fasta prot.fasta        # OpenFold3 (Apache-2)
bio-submit rfaa      --fasta prot.fasta         # RoseTTAFold-All-Atom, single-seq/template-free
bio-submit af3       --json fold.json           # BLOCKED unless gated af3.bin on the share
# Design / LM:
bio-submit rfdiffusion --contigs '[100-100]' [--num-designs 4] [--input-pdb t.pdb]
bio-submit mpnn --pdb backbone.pdb [--num-seqs 8]
bio-submit esm  <score|embed|logits|mutate> --fasta prot.fasta [--model M]
# common: --gpu <type> (see `dc types --gpu`), --spot, -- <extra passthrough>
# results: /var/lib/bio-runs/<jobid>/   (head-local; NOT the shared FS — see below)
```

Per-tool venvs build against the node's **system python3.12** (portable across
ephemeral nodes) except RFdiffusion/RFAA, which use a relocatable **py3.10** copied
onto the share (their old torch has no cp312). Weights/caches persist on the share,
so first run per tool is slow (installs + weights) and later runs reuse the cache.

**Why results go to head-local, not the shared FS:** the DataCrunch shared FS is
read/written fine *between GPU nodes* (so env/weight caching works), but the NixOS
head reads *node-written* files as zero-filled (an asymmetric NFS incoherence,
verified: the data is durable on the server — a node re-reads it after dropping its
cache — the head just can't see it). So `bio-submit` pulls outputs off the node via
`rsync` to `/var/lib/bio-runs/` while the node is alive.

**Status:** **Boltz-2 validated end-to-end** (real PDB retrieved). The others share
the identical framework (recipe + launch/fallback/sshd-wait/rsync-pull/teardown)
and are implemented + deployed but not each individually shaken out — a first run
per tool may need a small tweak (as Boltz-2's framework bring-up did). AF3 inference
stays blocked on gated weights; place `af3.bin` in `weights/alphafold3/` to enable.

## Credentials & security

- DataCrunch API creds: `/root/.config/datacrunch/credentials.env` (0600, **not** in
  this repo). SSH automation key: `/root/.ssh/datacrunch_ed25519` (0600) — lets the
  head SSH into the GPU nodes it launches.
- These are secrets living on a cloud box. Harden later with **sops-nix**/**agenix**
  (encrypted in the repo) rather than scp'd plaintext, scope the API key, and rotate.

## Install / update

```bash
# first install (onto a fresh Ubuntu instance):
nix run github:nix-community/nixos-anywhere -- --flake .#head --target-host root@<ip> \
  --ssh-option IdentityFile=~/.ssh/datacrunch_ed25519

# update after editing this config (from the workstation):
export NIX_SSHOPTS="-i ~/.ssh/datacrunch_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=~/.ssh/known_hosts_dc"
nixos-rebuild switch --flake .#head --target-host root@31.56.109.100
```

Defined as `nixosConfigurations.head` in `flake.nix` (standalone, not via the
desktop `mkSystem`). Disk layout in `disko.nix`; the `dc` CLI in `dc.sh`.
