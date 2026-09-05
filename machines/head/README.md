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

Next layer (not yet built): a `bio-submit <model> <input>` wrapper that picks the
right GPU tier per model, runs the model container, retrieves outputs, and
destroys the node — plus shared storage for the AF3/RFAA databases so they're
downloaded once. The `dc` primitives above are what it will build on.

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
