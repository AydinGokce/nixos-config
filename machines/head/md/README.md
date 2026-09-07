Molecular dynamics runs use the same durable Workbench queue, actor-scoped SSH
transport, ten-job burst capacity, artifact store and managed cloud leases as
folding. `bio-md` is installed on bio-head. No additional service, public port,
API key or permanently rented GPU is required.

Available routes are `pmx_binding_ddg`, `plumed_metadynamics` and
`bfee3_geometric`. Read [PROTOCOLS.md](PROTOCOLS.md) for their physical assumptions,
input contracts and interpretation. [runtime/README.md](runtime/README.md)
records exact package/source pins, licenses and native engine qualification.
The JSON files in [examples](examples) are protocol templates; their paths,
atom indices, selections and durations must be chosen for the actual system.

For canonical protein, DNA and RNA PDB structures, `bio-md prepare` builds a
solvated, ionized GROMACS system on the CPU head. It requires explicit chemistry
decisions, records original atoms/chains and every native command, and executes
no dynamics. For example, `decisions.json` for a structure without histidines or
disulfides is:

```json
{"protein_termini":"charged","nucleic_termini":"5prime_OH_3prime_OH",
 "side_chains":"standard_charged","hydrogens":"rebuild",
 "disulfides":"none","histidines":{}}
```

```sh
bio-md prepare --input complex.pdb --out ./prepared-system \
  --force-field amber99sb-star-ildn-mut --water-model tip3p \
  --salt-molar 0.15 --box-margin-nm 1.2 --temperature-kelvin 300 \
  --ph 7.4 --protonation-review 'Reviewed residue states and termini' \
  --chemistry-decisions decisions.json
```

These explicit states must match the intended molecule; specifying pH records
the assumption and does not calculate protonation. Every histidine needs an
explicit residue choice. Nonstandard caps, disulfide bonds, modified residues
and synthetic nucleotide chemistry require external preparation and validated
compatible parameters. Supply the incorporated nucleotide's complete chemistry,
including stereochemistry, charge and neighboring linkages; a synthesis amidite
name alone does not define the simulated residue. The helper never substitutes
a canonical nucleotide for a modified one. Its receipt and `assets/` directory
feed the protocol templates. BFEE separation boxes usually need substantially
more room than the simple padding in this example.

On the head, inspect a request without submitting it:

```sh
bio-md catalog
bio-md plan --request request.json --assets ./assets --out ./plan-review
bio-md validate --request request.json --assets ./assets \
  --name 'Editor / binding partner — mutation study' \
  --timeout 7200 --receipt ./preview-receipt.json
bio-md status --batch BATCH_ID
```

`plan` produces the complete command graph locally. `validate` uploads immutable
input copies and creates a durable preview. The head then runs native GROMACS
topology preparation and chemical-parameter checks without dynamics or paid
compute. BFEE3 additionally checks the actual partner selections and separation
box. Reuse the same receipt file to retry an interrupted validation request.
The preview must report compatible before submission:

```sh
bio-md submit --batch BATCH_ID --request-key unique-stable-commit-key
bio-md status --batch BATCH_ID
bio-md logs --job JOB_ID
bio-md artifacts --job JOB_ID
bio-md export --job JOB_ID --out ./original-results
bio-md cancel --batch BATCH_ID
```

`submit` returns after durable queueing; closing SSH does not stop the job.
The default worker choice tries compatible A100, L40S and H100 capacity under
the same spending guard. Set `--worker` during validation to require one exact
type or `CPU.16V.64G`. Failed allocations may select another compatible type;
a simulation that has started is never silently retried.
Identical submission retries reuse their original key. Each allocated worker
restores a checksum-verified, prebuilt CPU or CUDA runtime onto local disk.
Native files and checkpoints live in its persistent per-job output directory
and are archived on the head. Neither model construction nor database searches
are part of an MD run. The low-level `bio-submit md` route has the same native
admission gate before it may rent a worker.

For a stopped, interrupted or cancelled run with a sealed checkpoint:

```sh
bio-md resume --job JOB_ID --request-key stable-resume-preview --timeout 14400
bio-md status --batch NEW_PREVIEW_ID
bio-md submit --batch NEW_PREVIEW_ID --request-key stable-resume-commit
```

A resume is a new explicit cloud lease under the identical protocol. The
server verifies original artifact hashes and the regenerated plan. The worker
skips completed stages only after verifying their files, and checks checkpoint
and PLUMED-history hashes before native continuation. Running/completed jobs,
changed protocols, missing history or unsealed crash checkpoints are rejected.
Files remain available for inspection; no simulation is silently restarted.
Increasing the physical sampling duration is a new protocol, not a checkpoint
resume under changed settings.

Reports retain sampling diagnostics even when no affinity can be estimated.
MBAR/BAR errors, time stability, overlap and effective sample sizes describe
sampling within one Hamiltonian. Independent-replica scatter and differences
between physical models remain separate. Use one explicit `comparison_id` for
the same target and directed mutation across force-field studies. Analysis and
custom-parameter validation can run on the head without allocating compute:

```sh
bio-md analysis --help
bio-md analysis gromacs --protocol analysis-protocol.json window-*/dhdl.xvg
bio-md analysis compare binding-ddg-reports.json --output comparison.json
bio-md parameters validate --manifest modified-residue.json --base-dir ./assets
```

The SSH RPC exposes `md.catalog` (including complete request templates),
`md.validate`, `md.plan`, `md.resume` and
`md.compare`. Submission/status/cancellation/artifacts continue to use the
ordinary `batch.*`, `job.*` and `artifact.read` methods. `md.validate` takes
`{request_key,name,request,assets,timeout?,worker?}`, where `assets` maps relative
filenames to completed upload IDs. `md.compare` takes retained cycle/ΔΔG JSON
artifact IDs; it never pools force fields into one replica ensemble.

Harrison exposes those methods with the `bio.` prefix. Its existing channel
progress board uses MD stages (equilibration, sampling, analysis), and retains
the same final render/file delivery behavior. Native MD archives include
topologies, trajectories, checkpoints and analysis within the existing Slack
size limits; omissions are explicit. `bio-md export` retrieves the full retained
data when a trajectory is too large for Slack. The current native GUI can browse
MD batches and final PDB artifacts; dedicated MD submission forms and trajectory
playback are not part of this server addition.

The [cumulative spending guard](../BUDGET.md) is $750 for folding, MD and existing
infrastructure together. Previous spending is preserved. This is an estimated
launch/lease guard, not a provider billing cap; retained head/storage continue
to accrue costs. Long production studies must fit the remaining allowance and
declare their sampling/replicate requirements explicitly.
