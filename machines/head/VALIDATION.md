# Validation — 2026-09-06

These are functional checks with small protein inputs. Production RFAA database
installation is tracked separately from tests with miniature databases.

## Resident execution and completed private comparison

The full private ColabFold database snapshot is downloaded, extracted and
indexed, including all five required components. All 42 private native
preparations finished. A managed session then completed an additional real
request and closed after its idle timeout while preserving the borrowed API.
Index residency was measured, not guaranteed: the recorded `mincore` observation
showed 49.1% of the approximately 700 GB of index pages resident. Full prefetch
and locking modes have implementation tests but were not exercised on this
production session.

The matched H100 comparison finished at 21:36:01 UTC: **72/72 runs, 264/264
structures and 36/36 public/private pairs**. The 70 resumed runs completed in
about 13 minutes, all on their first queue attempt. Two earlier completed runs
remain in the cohort; the original failed OpenFold3 attempt is retained
separately. Independent audits verified every expected sample, file hash,
worker/GPU/configuration binding and CPU score. This elapsed time describes the
three parallel resident workers, not a controlled speedup experiment.

| Model | Public CA RMSD (Å) | Private CA RMSD (Å) | Public lDDT-CA | Private lDDT-CA |
|---|---:|---:|---:|---:|
| Boltz2 | 1.239559 | 1.237539 | 0.957496 | 0.957494 |
| OpenFold3 | 1.100616 | 1.123167 | 0.958774 | 0.958295 |
| Protenix | 1.223770 | 1.207792 | 0.955765 | 0.956680 |

These are descriptive means across all configured samples of 12 single chains,
88–502 residues. Small average differences do not establish equivalence for
large engineered editors, complexes or binding/inhibition. Public preparation
remains the default. The complete case/sample tables and preserved failures are
under `~/bio-runs/msa-resident-resume-20260906/v1/report-support/`; the independent
retention audit is under `audit-support/` in the same run directory.

Actual native A→B→A qualification preserved feature inputs, random-number state,
model parameters and buffers. Boltz matched coordinates and confidence exactly.
Protenix, OpenFold3 and RF3 showed small native CUDA numerical variability;
earlier strict coordinate-equality failures remain recorded. They were not
reclassified as passes by increasing the tolerance. RF3's final qualification
also verified all 15 sample chemistry audits, and its normal resident frontend
completed five more chemically valid samples with CPU validation before
publication. This establishes execution isolation and chemistry handling,
not general predictive or functional accuracy.

Two real queue requests reused one loaded model and survived a head coordinator
restart without changing attempt identity. A dedicated NFS control mount
reduced the observed worker-to-head result collection delay from 8–25 seconds
to 0.7–1.3 seconds. Cached artifacts separately passed actual NFS publication,
concurrent publication, writable-copy isolation and interrupted-publication
checks. GPU runtimes are sealed worker-local copies; no package installation is
performed per prediction. See [the execution contract](inference/README.md).

## Project briefs and construct descriptions

An actual workstation-to-head integration test created an isolated protein and
project, retained the original CRLF Markdown bytes, revised the purpose without
changing chemistry, and confirmed that project membership stayed pinned until
explicitly revised. Both project contexts were exported and verified locally.
Editing analysis notes remained valid; changing a frozen brief was rejected.
The same project-containing library passed backup and isolated restore. A real
RF3 CPU parser compiled its member and bound exactly the molecular snapshot
recorded in the exported context. No model inference or real research records
were created by this test.

Evidence: `~/bio-runs/project-context-validation-20260906/run-b2e44dc1ce/proof.json`.
Local tests cover safe archives, immutable dependency closure, concurrent
publication, missing descriptions, wrong-project transfers, both file-option
spellings and backward-compatible backup restore. Templates and an introduction
are installed at `~/bio-projects/`; the authoritative production library remains
on the head.

## RF3 and default language-model validation

RF3's pinned Foundry runtime and 3.04 GB checkpoint are installed. Real H100
predictions at 10 recycles, 50 diffusion steps, five samples and seed 101 passed
for ubiquitin, paired insulin A/B, and a six-chain assembly containing an
MSE-modified protein, modified DNA/RNA, charged/chiral ligands and zinc.
All five six-chain samples retained 777 finite atoms, exact named atom/element/
charge/bond inventories and 339 native tetrahedral constraints. The selected
sample's protein CA RMSD to 1UBQ was 1.0364 Å across 76 residues. This mixed input
is an installation fixture, not a demonstrated functional complex.

The earlier seven-chain fixture also requested an E-configured alkene; all five
predicted samples flipped it to Z. That original run and its native execution
status remain intact, with a separate failed chemistry audit. Mandatory output
QA now preserves all raw predictions and original rankings, publishes only the
highest native-ranked passing sample, and returns failure if none passes. Ten
retained ubiquitin/insulin samples passed the same audit. The desktop wrapper
verifies and renders only the audited selection.

Evidence is under `~/bio-runs/rf3-runtime-20260906/`,
`~/bio-runs/rf3-output-qa-20260906/` and
`~/bio-runs/rf3-validation-20260906/`; the latter includes the final deployment
proof, local render, exact cleanup proof for the normal submission, and wrapper
installation proof. Positive validation reused the existing MSA builder's GPU;
its lifecycle remains owned by that separate managed job. RF3 code is committed
in `268b13c`.

ESM and EVOLVEpro's default 650M checkpoint was downloaded with verified file
hashes and tested with networking disabled on an existing H100. Six sequence
scores and a finite 6 × 1280 embedding array passed; ranking selected two of
three unmeasured candidates. Evidence: `~/bio-runs/default-models-20260906/`.
Optional 3B/15B checkpoints remain downloads on demand.

These checks establish execution and the stated integrity properties. They do
not establish public/private database prediction-quality parity, binding,
biochemical activity or project-level success. RFAA's full database installation
is paused and its existing data retained.

## Cloud runs

Results are retained under `~/bio-runs/` on the workstation and
`/var/lib/bio-runs/` on the head. Each new job records its worker, timeout and
exit status in `job.json`; the log records confirmed worker/disk cleanup.

| Model | Cloud evidence | Status |
| --- | --- | --- |
| OpenFold3 0.5.0 | A100 80 GB; remote MSA; five CIFs, each 20 residues / 153 atoms with finite coordinates; local PyMOL PNG | Passed; worker and OS disk removed |
| RFAA single-sequence | A100 80 GB; expected 20-residue sequence / 301 atoms, all coordinates finite | Passed; worker and OS disk removed |
| ProteinMPNN | H100 80 GB; expected reference plus two 20-residue designs, finite scores | Passed; worker and OS disk removed |
| ESM-2 8M | A100 80 GB; six 21-residue variants with finite scores | Passed; worker and OS disk removed |
| EVOLVEpro-style ranking | H100 80 GB; six × 320 finite embeddings, three unmeasured variants ranked, two selected | Passed; worker and OS disk removed |
| RFdiffusion | A100 80 GB; 50-step inference, 50 residues / 200 finite backbone atoms; local render | Passed; worker and OS disk removed |
| Boltz-2 2.2.1 | A100 baseline and RTX PRO 6000 pinned runtime; 20 expected residues / 153 atoms; optimized CUDA kernels, workstation fetch and render | Passed; workers and OS disks removed |
| Protenix 2.0.0 | A100 80 GB; remote MSA; five CIFs, each 20 expected residues / 154 finite atoms; optimized inference and local render | Passed; worker and OS disk removed |

OpenFold3 job: `openfold3-20260906-005945-19991`. Local output:
`~/bio-runs/openfold3-final-validation-20260906/`, including `validation.json`
and `render.png`. Its completion guard also passes against the real outputs and
rejects failed, partially failed, and missing-structure fixtures. The upstream
runner can otherwise catch individual query errors and exit zero.

RFAA job: `rfaa-20260906-010605-20365`. Local output:
`~/bio-runs/rfaa-final-validation-20260906/`, including `validation.json` with
sequence/coordinate checks and artifact hashes. The CUDA graph check and full
model inference both succeeded on the fresh worker.
This single-sequence smoke test had modest confidence (mean CA score about 0.55)
and rough backbone geometry. It validates execution and output retrieval, not
structural accuracy; use full MSA/template mode for the production validation.

ProteinMPNN job: `mpnn-20260906-011251-20627`. Local output:
`~/bio-runs/mpnn-final-validation-20260906/`, including designs in `seqs/input.fa`
and parsed sequence/score checks in `validation.json`.

ESM job: `esm-20260906-012236-21879`. Local output:
`~/bio-runs/esm-final-validation-20260906/`. These are unmasked per-residue
log-probability scores, not masked pseudo-log-likelihoods.

EVOLVEpro job: `evolvepro-20260906-012443-21880`. Local output:
`~/bio-runs/evolvepro-final-validation-20260906/`, including `validation.json`,
embeddings, rankings, and selected FASTA. The random forest trained on three
measurements; selected variants `m5` and `m3` match the top two ranked candidates.
This checks the pipeline with a small fixture, not predictive performance.

RFdiffusion job: `rfdiffusion-20260906-021435-24266`. Local output:
`~/bio-runs/rfdiffusion-final-validation-20260906/`, including `design_0.pdb`,
`design_0.trb`, `validation.json`, and `render.png`. The worker verified the
transmitted code bundle, repaired the three missing CUDA 11 runtime wheels,
and passed the real DGL/SE3 CUDA graph preflight. Inference produced the expected
50-residue backbone in about 25 seconds; all 200 atoms have finite coordinates.
The saved model confidence is not an experimental accuracy measurement.

Initial Boltz job: `boltz2-20260906-013905-21928`. Local output:
`~/bio-runs/boltz2-final-validation-20260906/`, including `validation.json` and
`render.png`. The workstation source wrapper submitted the query, retrieved the
results, and rendered the structure successfully. The recipe's output guard ran
on the worker and also rejects missing/empty artifacts in fixtures. Runtime
dependency pins were then tested in job `boltz2-20260906-014800-23248` on an RTX
PRO 6000. Output: `~/bio-runs/boltz2-pinned-validation-20260906/`, including
`validation.json` and `render.png`. The exact Torch 2.8.0+cu128, Triton 3.4.0 and
cuEquivariance 0.6.1 stack passed all three optimized triangle-kernel preflights
and inference. The expected 20-residue, 153-atom structure has finite coordinates
and a complete backbone; worker and OS absence were independently checked.

Protenix and RFdiffusion retries exposed stale recipe contents on the mutable
shared filesystem. Submissions now transmit deployed code over SSH, verify its
SHA-256, and run it from worker-local storage. `tools.tar.gz` and `tools_sha256`
in `job.json` identify the executed snapshot. RFdiffusion's successful run and
Protenix's successful compiled-kernel check both exercised this path.

Protenix job: `protenix-20260906-022553-24995`. Local output:
`~/bio-runs/protenix-final-validation-20260906/protenix-20260906-022553-24995/`,
including `verification.json` and `render.png`. The pinned CUDA toolchain,
compiled LayerNorm, ColabFold search and optimized model inference all passed.
All five structures contain the exact 20-residue sequence and complete backbones;
coordinates and confidence outputs are finite. Template mode was disabled.
The raw MSA response, individual alignments, original/prepared inputs, effective
configuration, actual command/environment, and checkpoint/CCD hashes are retained
for private-backend comparison. These are functional checks, not an accuracy
benchmark. Independent inventory confirmed worker and OS deletion.

The provider's storage quota includes trashed OS volumes. The quota regression
was resolved by permanently removing seven disposable OS disks (350 GB) whose
IDs and names matched closed managed jobs. The 21 older unmatched trash disks,
head OS and original shared volume were preserved. New managed worker cleanup
now confirms permanent OS removal; the ESM run exercised this path successfully.

## NFS read compatibility

The same persisted 1739-byte ColabFold `manifest.json` read as all NUL bytes on
the head over NFS 4.2, with SHA256
`0c7c4f620e26e38293d8c519ada2701da93058ff12089cd1dfba33d25e9af8b7`.
After explicitly unmounting all three shares and deploying generation
`6yksd26rbahkzchk7xhf98dl1zbma39q`, actual `findmnt` options on the head showed
`vers=4.1` for the original share and both database volumes. The manifest then
parsed as JSON equal to the pinned installer manifest, with SHA256
`c41c662d09ba43e090e64301410a15185f7a2b1a516ad533166ede805c55694e`.
The existing 7605-byte Boltz `mols/ALA.pkl` also read as nonzero content, with
SHA256 `00c247b6c8e5e3c2d248ab59560219bb35fe801ba8a8b52a5d76dc198a8d984b`.

Independent read-only confirmation at 2026-09-06 03:08:52 UTC is retained in
`~/bio-runs/nfs41-validation-20260906/evidence.json`, including actual mounts,
file hashes, generation and service invocation IDs. RFAA and MSA download
services resumed at 03:04:57 UTC; their growing partial archives had gzip
headers. These observations do not establish completed downloads, index creation
or whole-corpus integrity.

Both head configuration and fresh-worker mount commands now explicitly request
NFS 4.1. An already-mounted worker is not changed merely by deploying a new
script. This is an observed read-compatibility workaround on this head/provider
pair, not proof of a particular kernel/server fault or validation of every
worker's filesystem view. Verified code transmission, worker-direct output
retrieval and database validation on each worker remain necessary.

The first public prepared Protenix replay subsequently stalled in one NFS `OPEN`
for SymPy bytecode on a worker. Fresh read-only NFS 4.1 and 4.2 mounts, and a new
read through the original mount, returned identical valid bytes immediately;
only the existing open request remained blocked. That attempt was stopped and
its worker/OS disk deletion confirmed. Fresh workers now set
`PYTHONPYCACHEPREFIX` to their local temporary tools directory before any Python
starts. Both subsequent Protenix reference cases completed with five structures
each. This supports cache isolation as a practical workaround, without proving
the cause of the earlier stuck request. Full evidence, failed-attempt records
and settings are retained under `~/bio-runs/msa-public-inference-20260906`.

## Local and simulated checks

- Budget controller: 59 tests for accounting, reservations, uncertain creates,
  deadlines, storage lifetime checks, protected-resource cleanup and retaining
  persistent-volume accounting during unexplained inventory omissions. Fresh
  hourly quote limits fail before reservations or resource creation; malformed
  limits also fail closed.
- Persistent database allocator: 18 tests cover serialized creation, durable
  intents, conservative quotes, uncertain-request reconciliation and retirement.
- Submission orchestration: 52 tests cover retrieval/failure propagation, budget
  denial, RFAA storage/memory requirements and expiry races, Protenix GPU and
  endpoint configuration, verified code delivery despite stale shared files or
  corrupted transfer, and cleanup after the log pipe closes during failures or
  termination. They also cover private preparation before inference rental,
  explicit NFS 4.1 mount arguments, and head-side database readiness checks
  before any paid launch. Complete panel manifests are validated before rental,
  checked again after the lock and on the worker, and all failures are retained.
  Workstation wrapper: six tests.
- Database storage lifecycle: 36 offline tests for exact-volume identity,
  concurrent expiry/launches, process identity across reboots, preserving
  completed outputs, confirmed cleanup/retry behavior, persistent retention and
  profile isolation. Both deployed receipts are now active and persistent;
  ordinary expiry timers leave them retained. No production retirement was
  performed for these tests.
- EVOLVEpro: seven numerical/input tests; actual CPU ESM-2 embeddings and
  regression ranked three candidates and selected two from six variants.
- RFAA: nine preparation/database tests and three installed-parser regression
  tests. Actual local CUDA single-sequence inference produced 20 residues / 301
  atoms. Real HHblits, PSIPRED and HHsearch using miniature read-only databases
  and 1UBQ template coordinates fed a successful CUDA prediction with 76 residues
  / 1228 atoms. Outputs/hashes: `~/bio-runs/rfaa-local-validation-20260906/readiness.json`.
- ProteinMPNN: actual local CUDA design produced two 20-residue designs plus
  reference; scoring-only output also passed. Missing-GPU and empty-output
  failures were detected.
- ESM: actual local CUDA score, embedding, logits and mutation-effect commands
  passed; scores have six finite rows. Multiple WT records for mutation scoring
  and unavailable CUDA are rejected. MPNN/ESM local outputs:
  `~/bio-runs/mpnn-esm-local-e8irlrry/`.
- Protenix dependencies: fresh Python 3.12 source build with NumPy 2.4.1,
  scikit-learn 1.7.1 and scikit-learn-extra 0.3.0 passes imports and KMedoids
  fitting. Reproduced a cached NumPy-1 ABI failure and verified uncached rebuild
  repairs it. Recipe fixtures reject missing/empty predicted CIF output.
- RFdiffusion: actual local CUDA graph operations and SE3/model-runner imports
  passed with the pinned legacy Torch/DGL stack.

Both head and workstation NixOS configurations build successfully. Head changes
are deployed. Workstation activation still requires the user's sudo
authentication. AF3 was excluded from changes and validation.

## Production databases and quality comparisons — earlier setup record

The following milestones precede the completed resident comparison above.

The 3300 GB RFAA volume `00537aea-2184-434a-84c1-1074bd1ebd58` and separate 3000 GB
ColabFold volume `3ccef50a-59fe-4a5f-b7d3-ec669fe7ccef` are allocated, registered
with persistent retention, and mounted on the head. Initially both archive
downloads ran on the head, with ColabFold conversion, full CPU indexes and mmCIF
mirroring reserved for a sufficiently large worker. RFAA was subsequently
parked with its data retained; ColabFold completed as recorded above. The miniature-database
results above do not establish production corpus completeness or scientific
parity. Persistent storage remains billable after the $500 launch/compute guard
halts new paid work; it is not a hard storage spending cap.

Six numerical scoring tests pass, covering proper rotation, mirror exclusion
from RMSD, local distortion, invalid coordinates and strict complete-sequence
prediction parsing, including rejection of multiple models and duplicate atoms.
The native OpenFold3 CIF omits occupancy. For predictions with no alternate atom
conformers, the parser supplies occupancy 1.0 in memory while preserving original
bytes; missing occupancy with alternate conformers still fails. Real OpenFold3
outputs now score, and prior Protenix scores remain exactly unchanged.
Six classical and twelve recent experimental references retain full construct
FASTA sequences, observed-residue mappings and source hashes. The recent panel
has a frozen selection/exclusion audit under
`~/bio-runs/msa-recent-panel-20260906`; this selection preceded its predictions.
Its engineered, truncated and fusion constructs are explicitly labeled.
See [the quality comparison protocol](msa/QUALITY.md). Public preparation remains
the default until appropriate comparisons support a change.

All six **public-MSA** reference replays are now complete: two experimental
targets (1UBQ and 2LZM), each with Protenix, OpenFold3 and Boltz2. All 22 generated
structures are scored, and every temporary worker and OS disk is removed. The
stopped first Protenix attempt remains in the report as a failed job. The report
verifies complete sequences, original structures and hashes, every expected
sample, native prepared inputs, runtime/package/checkpoint identity and resolved
settings. Nine report tests cover omissions, mutations and aggregation. Attempt
coverage is explicitly unverified without an immutable prelaunch attempt ledger.

The retained result is
`~/bio-runs/msa-public-inference-20260906/public-baseline-report.json`. This is a
public baseline against experimental structures, **not a comparison with full
private databases**. Both public Protenix settings snapshots must be reconstructed
with the final frozen helper on the next naturally available matching A100 before
strict private comparison; earlier helper snapshots remain retained. Boltz
settings were reconstructed on the head with the exact package snapshot, without
renting another GPU or running inference.

All 42 public native preparation bundles for the frozen 14-case, three-model
panel are validated. One recent Boltz target had a transient HTTP 404; the
original failure and a successful explicitly recorded identical-input retry are
both retained. The 12 recent cases were then ready for GPU prediction. Their frozen
private panel is `~/bio-runs/msa-private-panel-20260906/panel-input.json`, canonical
SHA256 `8bb1eb328c2bd0258b3e868be3f0c496e456f31c6e646e3428e8812d882334f0`.

Private panel preparation passes 11 tests, including actual localhost proxy
isolation and retained official paired-job archive evidence. The resumable build
queue passes 35 tests for download prerequisites, promoted-component resumption,
capacity/price limits, uncertain starts, exact cleanup and panel-only retries.
A head-only systemd probe confirmed retained process/invocation metadata used
for reconciliation. Those implementation checks established orchestration
behavior; corpus completion and the limited comparison are recorded above. See
[the panel interface](msa/PANEL.md) and [the build queue](msa/BUILD_QUEUE.md).
