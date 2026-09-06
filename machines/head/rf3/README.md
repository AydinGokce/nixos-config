# RF3 runtime and input preparation

RF3 runs in its own shared-storage environment and uses an explicit MSA for every
protein chain. The head prepares and validates inputs before requesting a folding
GPU. The worker receives the complete prepared directory, retains its input and
search provenance, and performs no additional sequence search.

The supported project route is `bio-fold rf3` with a protein FASTA or a
construct/assembly reference. Library compilation checks the actual RF3/AtomWorks
chemistry parser and native model features on CPU. It preserves protein, DNA,
RNA, supported CCD modifications, and supported ligand chemistry. A successful
CPU check establishes input compatibility; it does not establish prediction
accuracy. The library adapter rejects chemical details that the native model
would discard, including ordinary polymer-to-polymer crosslinks. Polymer chains
shorter than four residues are rejected because the pinned model removes them.
Circular peptides use RF3's explicit `cyclic_chains` runtime option; circular DNA
and RNA are not currently exposed. See [the library documentation](../library/README.md)
for construct identity, modification, and bond requirements.

## Pins and persistent files

| Component | Pinned identity |
| --- | --- |
| Official Foundry source | `b02eed6a6bdf8f44d14a80cc36e3da13c9f2291c` |
| Python | 3.12; Ubuntu worker system interpreter |
| AtomWorks | 2.2.1 |
| Torch | 2.7.1+cu128 |
| cuEquivariance packages | 0.6.1 |
| Dependencies | [requirements.lock](requirements.lock), exact versions and distribution hashes |
| Checkpoint | `rf3_foundry_01_24_latest_remapped.ckpt`, latest recommended variant, January 2024 training cutoff |
| Checkpoint size | 3,038,876,446 bytes |
| Checkpoint SHA256 | `364ef592fd8042a9cf4176d045015190f8322f961ccca38d891b20ca578d3bb0` |

The source and installed RF3/Foundry Python/YAML files are compared before
prediction. The checkpoint comes from the official [IPD HTTPS download](https://files.ipd.uw.edu/pub/rf3/rf3_foundry_01_24_latest_remapped.ckpt),
as registered in [the pinned Foundry checkpoint registry](https://github.com/RosettaCommons/foundry/blob/b02eed6a6bdf8f44d14a80cc36e3da13c9f2291c/src/foundry/inference_engines/checkpoint_registry.py).
Upstream does not publish a checkpoint SHA256 there; the value above was computed
from the official HTTPS download on September 6, 2026 and is enforced thereafter.
Foundry's [license](https://github.com/RosettaCommons/foundry/blob/b02eed6a6bdf8f44d14a80cc36e3da13c9f2291c/LICENSE.md)
is BSD-3-Clause; Rosetta Commons also identifies RF3 as BSD-3-Clause on its
[official download page](https://rosettacommons.org/software/download/).

Persistent paths are `/mnt/bio-shared/envs/rf3`,
`/mnt/bio-shared/src/foundry-rf3-<commit>`, and
`/mnt/bio-shared/rf3/checkpoints`. The recipe reuses an environment only after
checking its exact dependency versions and source files. Installation affects
these RF3 paths and does not edit another model's environment or the MSA mirror.

## MSA preparation and pairing

`msa.py` obtains filtered UniRef and environmental alignments using the existing
ColabFold-compatible API contract. Raw responses, request tickets, exact chain
queries, source metadata, and checksums remain in the prepared result. Private
searches require the database provenance produced by the managed private MSA
workflow. Public and private sources remain explicitly distinguishable.

For a heteromer, the server's `pairgreedy` result defines which homolog rows
belong together. Its paired row order is encoded in shared, descending 19-digit
`TaxID` fields because AtomWorks uses that field as its pairing key. **These are
synthetic pair identifiers, not biological taxonomy IDs.** Rows missing a chain
are omitted from that chain's paired input, so AtomWorks supplies the correct
padding mask. Original unpaired sequences, insertion letters, and headers remain
unchanged. Raw paired results are retained as evidence. Native tests cover partner
identity, ordering, insertion counts, partial pairing, and dense/sparse packing.

Preparation retains all received alignment rows. The native RF3 loader and MSA
featurizer apply their own pinned depth limits and sampling. Each prediction's
`rf3-features.json` records the actual loaded per-chain depth, pairing counts,
native depth limits, and per-recycle feature shapes. `rf3-runtime.json` binds this
audit to the prepared-input digest, checkpoint, runtime, and inference settings.
The audit observes native transform results without changing their arrays or
random-number state.

For operator-supplied alignments, run on the head or another machine with Python:

```sh
python3 /etc/bio-tools/rf3/prepare.py prepare \
  --fasta query.fasta --msa-map chain-msas.json --out prepared
python3 /etc/bio-tools/rf3/prepare.py validate --input prepared/input.json
```

`chain-msas.json` maps every protein chain ID to its A3M path, for example
`{"A":"/absolute/query.a3m"}`. FASTA chains are assigned `A`, `B`, … in input
order; original headers are retained. `--native-json` accepts one already
compiled RF3 assembly instead of FASTA, retaining explicit polymer types and
parenthesized CCD tokens such as `"(MSE)"`. Standard amino acids map to their
one-letter MSA query; noncanonical residues map to `X`. Gzip A3Ms are copied
without recompression. Missing, mismatched, or ambiguous alignments fail; there
is no automatic sequence-only fallback. A deliberately supplied query-only A3M
is identified as depth one and is not described as a homologous alignment.

## Worker setup and recovery

On a managed Ubuntu 24.04 CUDA worker, with `/mnt/bio-shared` mounted, `uv`, Git,
and the current toolkit already staged:

```sh
bash "$BIO_TOOLS_DIR/rf3/install.sh" /mnt/bio-shared
/mnt/bio-shared/envs/rf3/bin/python "$BIO_TOOLS_DIR/rf3/runtime.py" download
/mnt/bio-shared/envs/rf3/bin/python "$BIO_TOOLS_DIR/rf3/runtime.py" check
```

Installation uses the committed dependency lock. Downloading uses bounded HTTPS
byte-range requests and publishes only after checking the full checkpoint SHA256.
A mismatched existing checkpoint is rejected rather than overwritten. The shared
venv points to the worker's `/usr/bin/python3`; CPU library checks on the NixOS
head use the separately configured Python 3.12 runtime and this environment's
site-packages. The shared venv executable itself is not a NixOS interpreter.

The tracked [compatibility helper](../library/rf3_compat.py) removes an existing
temporary `atom_id` annotation from the parser's private pipeline-input copy.
This satisfies the native transform that creates fresh global atom indices. It
verifies the pinned source and preserves every other atom annotation, coordinate,
charge, and bond. The identical helper runs in CPU preflight and GPU inference;
its identity and hash are recorded.

## Prediction settings and verification

Defaults preserve the official inference settings: 10 recycles, 50 diffusion
steps, five samples, and native early stopping. This integration fixes seed 101
for repeatability and uses one GPU. Bounded `n_recycles=`, `num_steps=`,
`diffusion_batch_size=`, and `seed=` overrides are supported. Input paths,
checkpoint selection, MSA guards, and device count cannot be replaced through
extra arguments. An early-stopped input without a structure is reported as an
incomplete prediction. Success requires a ranked CIF, a finite native ranking
score, the native feature audit, and the mandatory output chemistry check.

The output check compares every sample with the native input's named atoms,
elements, formal charges, bonds, finite coordinates, and assigned tetrahedral and
double-bond stereochemistry. It preserves all raw samples and the original
native ranking files, then publishes the highest native-scoring sample that
passes those checks. If no sample passes, the workflow fails and retains the
diagnostics. It does not repair coordinates, change the native confidence scores,
or rerun inference. `rf3-output-validation.json` records the selection and checks;
`rf3-output-expected.json` retains the expected native chemistry. The runtime
manifest binds both the validation source and its result by hash.

Input compatibility does not guarantee the predicted coordinates preserve every
stereochemical constraint. In one seven-chain validation fixture, all five
default samples inverted an explicitly specified E alkene despite correct input
reference features. That fixture is retained as a failed output validation case.
Passing the output check does not establish accurate folding, binding, or general
bond geometry.

The initial H100 validation used default settings and a public MSA for 76-residue
ubiquitin: pTM 0.93895, mean pLDDT 0.9186, no predicted clashes, and 0.6301 Å CA RMSD
to PDB 1UBQ across all 76 residues. All 601 output atom coordinates were finite.
Its ranking score 0.1878 is expected from this release's `0.8*iPTM + 0.2*pTM`
formula because it reports monomer iPTM as zero. This is one installation check
on a familiar structure, not an accuracy benchmark or evidence of equivalence
between public and private database outputs.

Offline tests run with `python3 -m unittest discover -s machines/head/rf3`.
The native pairing tests run in the pinned RF3 environment and otherwise skip.
Library native chemistry checks and end-to-end GPU predictions are separate
verification steps.
