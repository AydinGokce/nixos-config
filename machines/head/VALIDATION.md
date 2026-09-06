# Validation — 2026-09-06

These are functional checks with small protein inputs. Production RFAA database
installation is tracked separately from tests with miniature databases.

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

## Local and simulated checks

- Budget controller: 41 tests for accounting, reservations, uncertain creates,
  deadlines, storage lifetime checks and protected-resource cleanup.
- Submission orchestration: 17 tests cover retrieval/failure propagation, budget
  denial, RFAA storage/memory requirements and expiry races, Protenix GPU and
  endpoint configuration, verified code delivery despite stale shared files or
  corrupted transfer, and cleanup after the log pipe closes during failures or
  termination. Workstation wrapper: six tests.
- Database storage lifecycle: 24 offline tests for exact-volume identity,
  concurrent expiry/launches, process identity across reboots, preserving
  completed outputs, and confirmed cleanup/retry behavior. The head's deployed
  expiry timer is active; without a receipt its service exits successfully and
  the full-mode receipt check refuses submission. No production volume was
  created for these checks.
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

## Production RFAA databases

Full database download and full production-database cloud inference remain
pending the database deployment phase. The user has authorized persistent
retention and a private MSA backend, with prediction quality as the acceptance
criterion before switching. No 3300 GB database volume has been allocated. The
miniature-database results above do not establish a successful production
download or exhaustive database integrity.
