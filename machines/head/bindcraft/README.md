# BindCraft on bio-head

`bio-bindcraft` prepares and submits binder-design jobs on temporary GPU workers.
It uses the existing spending guard, worker timeout, runtime archive cache and
automatic cleanup. No MSA service or sequence database is required. BindCraft
designs protein binders against an input target structure; it is a separate
design workflow from the console's folding-model selection.

The installation is isolated at `/mnt/bio-shared/bindcraft`. Its Python, CUDA
libraries, ColabDesign/ProteinMPNN, PyRosetta, DSSP, DAlphaBall and all 15 AF2
parameter sets are pinned. A worker restores a verified runtime archive onto
local disk at the same absolute paths. It never installs or resolves packages
during a design job. Existing folding environments and MSA databases are not
modified. Source revisions, installation evidence, input hashes, effective
settings, logs and model outputs are retained with each job.

## Usage

On bio-head:

```sh
bio-bindcraft doctor
bio-bindcraft prepare --pdb target.pdb --chains A --hotspots A56 \
  --name target_binder --lengths 65 150 --designs 100 --out target.tar.gz
bio-bindcraft validate --bundle target.tar.gz
bio-bindcraft submit --bundle target.tar.gz --timeout 7200
```

`prepare` and `validate` allocate no compute. The bundle contains exact copies
of the PDB, target settings, design settings and filters. Paths to runtime
assets and outputs are managed by the cluster. Selected target chains must be
canonical proteins; modified residues and nucleic-acid targets are not silently
converted. Use one PDB model and renumber insertion codes first. Hotspot numbers
refer to the original target PDB residue numbers.

To edit the native settings, export the pinned defaults and pass the edited
files to `prepare`:

```sh
bio-bindcraft defaults advanced > advanced.json
bio-bindcraft defaults filters > filters.json
bio-bindcraft prepare --pdb target.pdb --chains A --name target_binder \
  --advanced advanced.json --filters filters.json --out custom.tar.gz
```

Normal design settings and filters default to the unmodified upstream four-stage
multimer protocol. The input bundle preserves all user changes. The optional
`max_trajectories` upstream setting counts relaxed trajectories rather than all
attempts; the cloud `--timeout` remains the hard execution bound. Failed attempts
and a shortage of accepted designs do not imply an installation failure.
Confidence scores and Rosetta interface scores are not experimental affinity
measurements.

The default GPU fallback order is A100 80 GB, L40S, H100, A100 40 GB, then A6000.
Use `--gpu TYPE` to require one type and `--spot` for spot rental. These CUDA 12
dependencies are not enabled on Blackwell workers. Large targets need more GPU
memory and time; crop targets only when scientifically appropriate. The current
$750 cumulative spending ceiling includes prior work and persistent storage.

`submit` follows the foreground `bio-submit` lifecycle. To keep a run alive
after closing SSH, start it as a transient head service:

```sh
systemd-run --unit=bindcraft-example --collect \
  bio-bindcraft submit --bundle /absolute/path/target.tar.gz --timeout 7200
journalctl -fu bindcraft-example
```

Results live in `/var/lib/bio-runs/bindcraft-DATE-TIME-PID` on the head and in
the matching shared run directory. `bindcraft-result.json` distinguishes run
completion from the count of accepted designs. PDBs, sequence/score CSVs,
effective settings and logs remain available even when no design passes the
filters. The Console has a dedicated binder-design workflow; ordinary folding
forms remain separate. Head RPCs validate uploaded/retained structures, preserve
crop/hotspot residue maps, and submit through the same managed launcher.

`prepare --seed N` adds a hashed campaign RNG seed without changing native
scientific settings or filters. `submit --max-cost-usd N` caps the freshly quoted
GPU plus disposable OS reservation. The Console additionally keeps fallback
attempts within one durable per-run cost scope. Native trajectory seeds, accepted
and rejected candidates, and exact target/settings provenance remain in outputs.

## Installation and qualification

The NixOS head configuration installs the orchestration command and pinned
installer. Model assets are persistent rather than embedded in the Nix closure.
The installer requires a dedicated cache and uses only committed package hashes:

```sh
python3 /etc/bio-tools/bindcraft/install.py \
  --prefix /mnt/bio-shared/bindcraft --cache /path/to/dedicated-build-cache \
  --evaluation
bio-bindcraft doctor --verify-assets
bio-bindcraft test --out /var/lib/bio-runs/bindcraft-test-input --timeout 3600
```

`test` rents one managed GPU and runs real numerical component checks against
the pinned upstream PDL1 example. Its reduced-iteration diagnostic structures
are installation evidence, not accepted production binder candidates. It checks
GPU execution, AF2 gradients and prediction, MPNN, PyRosetta relaxation/interface
scoring, DAlphaBall and DSSP, and writes timings and artifact hashes.

The user explicitly requested PyRosetta installation for evaluation on
2026-09-13; the installation receipt records the commercial license as pending.
The `--evaluation` flag records that requested scope and does not acquire a
license or claim a testing exemption. BindCraft is MIT licensed; PyRosetta's
official terms require a separate commercial agreement for company use.
[BindCraft](https://github.com/martinpacesa/BindCraft),
[PyRosetta downloads and licensing](https://www.pyrosetta.org/downloads).

The vendored default JSON files are from BindCraft revision
`efb5bfeb8b4b1a5944256f979c34e0c8e6a82d9d`, with its MIT notice in
`defaults/LICENSE.BindCraft`. Exact other package/source/weight hashes are in
`pins.json` and `linux-64-cuda.lock.json`.
